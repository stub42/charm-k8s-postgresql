# This file is part of the PostgreSQL k8s Charm for Juju.
# Copyright 2020 Canonical Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3, as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranties of
# MERCHANTABILITY, SATISFACTORY QUALITY, or FITNESS FOR A PARTICULAR
# PURPOSE.  See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import logging
import os
from pathlib import Path
from typing import Iterable

from charmhelpers.core import host
import kubernetes
from kubernetes.client.rest import ApiException as K8sApiException
import ops.framework
import ops.model

from connstr import ConnectionString
from leadership import RichLeaderData
import pg


PG_MAJOR = 12


log = logging.getLogger(__name__)


class ClientRelations(ops.framework.Object):
    def __init__(self, charm, key):
        super().__init__(charm, key)
        self.charm = charm
        self.passwords = RichLeaderData(self, "passwords")

        self.unit = self.model.unit
        self.app = self.model.app

        self.framework.observe(charm.on["db"].relation_changed, self.on_db_relation_changed)
        self.framework.observe(charm.on["db-admin"].relation_changed, self.on_db_admin_relation_changed)

    _authed = False

    def k8s_auth(self):
        if self._authed:
            return
        # Remove os.environ.update when lp:1892255 is FIX_RELEASED.
        os.environ.update(
            dict(e.split("=") for e in Path("/proc/1/environ").read_text().split("\x00") if "KUBERNETES_SERVICE" in e)
        )
        kubernetes.config.load_incluster_config()
        self._authed = True

    @property
    def master_service_name(self) -> str:
        return f"{self.app.name}-master"

    @property
    def standbys_service_name(self) -> str:
        return f"{self.app.name}-standbys"

    @property
    def master_service_ip(self) -> str:
        svc = self.get_k8s_service(self.master_service_name)
        if svc is None:
            return None
        return svc.spec.cluster_ip

    @property
    def standbys_service_ip(self) -> str:
        svc = self.get_k8s_service(self.standbys_service_name)
        if svc is None:
            return None
        return svc.spec.cluster_ip

    def get_k8s_service(self, name):
        self.k8s_auth()
        cl = kubernetes.client.ApiClient()
        api = kubernetes.client.CoreV1Api(cl)
        try:
            return api.read_namespaced_service(name, self.model.name)
        except K8sApiException as e:
            if e.status == 404:
                return None
            raise

    def on_db_admin_relation_changed(self, event):
        self.on_db_relation_changed(event, admin=True)

    def on_db_relation_changed(self, event, admin=False):
        # Database username is the remote Application name.
        username = event.app.name

        password = self.db_password(username)
        master_ip = self.master_service_ip
        standbys_ip = self.standbys_service_ip
        if not password or master_ip is None or standbys_ip is None:
            # Leader will set the password and create the service in
            # the first hook. But maybe invalid config caused
            # pod-set-spec to not be run yet, or similar.
            log.info("Waiting for leader")
            event.defer()  # Wait for leader to choose the password.
            return

        # Inspect requests from the client. First look in Application
        # data for modern clients. Fall back to eventually consistent
        # unit data.
        for bucket in [event.relation.data[event.app], event.relation.data[event.unit]]:
            dbname = bucket.get("database", "")
            sroles = bucket.get("roles", "")
            roles = list(_csplit(sroles))
            sextensions = bucket.get("extensions", "")
            extensions = list(_csplit(sextensions))
            if dbname or roles or extensions:
                log.info(f"Client requested {dbname=} {roles=} {extensions=}")
                break

        # Fall back to a database named after the remote Application.
        # This is problematic for cross-model relations, where the
        # remote Application name may have been anonymized and we
        # get a unique database name every fresh deployment.
        if not dbname:
            dbname = event.app.name

        is_leader = self.unit.is_leader()
        if is_leader:
            con = pg.connect(
                ConnectionString(
                    host=master_ip, dbname="postgres", user="postgres", password=self.charm.get_admin_password()
                )
            )
            pg.ensure_user(con, username, password, superuser=admin)
            pg.ensure_db(con, dbname, username)
            pg.ensure_roles(con, roles)
            pg.ensure_extensions(con, extensions)

        # Publish allowed-subnets to the relation, listing the
        # egress-subnets that have been granted access.
        # TODO: Meaningless in the k8s PostgreSQL charm. Can we have the
        # k8s Service limit connections? Do we want to?
        allowed_subnets = self.get_allowed_subnets(event.relation)
        allowed_units = self.get_allowed_units(event.relation)  # Legacy protocol, deprecated
        port = 5432

        # Publish connection details to the master.
        master = ConnectionString(
            host=master_ip,
            dbname=dbname,
            port=port,
            user=username,
            password=password,
            fallback_application_name=event.app.name,
            sslmode="prefer",
        )
        standbys = ConnectionString(
            host=standbys_ip,
            dbname=dbname,
            port=port,
            user=username,
            password=password,
            fallback_application_name=event.app.name,
            sslmode="prefer",
        )

        # Echo back data to clients so they know their requested changes
        # have been made. On Application data for modern clients, and
        # unit data for backwards compatibility.
        to_publish = [event.relation.data[self.unit]]
        if is_leader:
            to_publish.append(event.relation.data[self.app])
        for bucket in to_publish:
            bset(bucket, "database", dbname)
            bset(bucket, "roles", sroles)
            bset(bucket, "extensions", sextensions)
            bset(bucket, "allowed-subnets", allowed_subnets)
            bset(bucket, "master", str(master))
            bset(bucket, "standbys", str(standbys))

            # Charm only supports PG 12. If we support other versions,
            # we would need to somehow extract this information from
            # the image or a running pod. Pods could label themselves
            # with the baked in version.
            bset(bucket, "version", str(PG_MAJOR))

            # Legacy protocol for antique clients, deprecated.
            if is_leader:
                bset(bucket, "host", master_ip)
                bset(bucket, "state", "master")
            else:
                bset(bucket, "host", standbys_ip)
                bset(bucket, "state", "hot standby")
            bset(bucket, "port", str(port))
            bset(bucket, "user", username)
            bset(bucket, "password", password)
            bset(bucket, "allowed-units", allowed_units)

    def db_password(self, username):
        if username not in self.passwords:
            if not self.unit.is_leader():
                return None
            self.passwords[username] = host.pwgen(40)
        return self.passwords[username]

    def get_allowed_subnets(self, relation) -> str:
        subnets = set()
        for key, reldata in relation.data.items():
            if "/" in key.name:
                subnets.update(set(_csplit(reldata["egress-subnets"])))
        return ",".join(sorted(subnets))

    def get_allowed_units(self, relation) -> str:
        return ",".join(sorted(unit.name for unit in relation.data if isinstance(unit, ops.model.Unit)))


# Workaround for https://github.com/canonical/operator/pull/399/files
def bset(bucket, key, value):
    if value or key in bucket:
        bucket[key] = value


def _csplit(s) -> Iterable[str]:
    if s:
        for b in s.split(","):
            b = b.strip()
            if b:
                yield b
