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
import ops
import yaml

from connstr import ConnectionString
from leadership import RichLeaderData


log = logging.getLogger(__name__)


class ClientRelations(ops.framework.Object):
    def __init__(self, charm, key):
        super().__init__(charm, key)
        self.charm = charm
        self.passwords = RichLeaderData(self, "passwords")

        self.unit = self.model.unit
        self.app = self.model.app

        self.framework.observe(charm.on.start, self.create_k8s_services)

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
        return self.get_k8s_service(self.master_service_name).spec.cluster_ip

    @property
    def standbys_service_ip(self) -> str:
        return self.get_k8s_service(self.standbys_service_name).spec.cluster_ip

    def create_k8s_services(self, event) -> None:
        """Create required k8s Services

        There doesn't seem to be a documented way of creating a K8s
        Service via pod-set-spec, so do it manually via the K8s API.
        """
        self.k8s_auth()

        # Instantiate a client and the API we need
        cl = kubernetes.client.ApiClient()
        api = kubernetes.client.CoreV1Api(cl)

        service = {
            "metadata": {"name": self.master_service_name},
            "spec": {
                "type": "NodePort",  # NodePort to enable external connections
                "external_traffic_policy": "Local",  # "Cluster" for more even load balancing
                "ports": [{"name": "pgsql", "port": 5432, "protocol": "TCP"}],
                "selector": {"juju-app": self.app.name, "role": "master"},
            },
        }

        log.info(f"master Service definition <<EOM\n{yaml.dump(service)}\nEOM")
        try:
            api.create_namespaced_service(self.model.name, service)
        except kubernetes.client.rest.ApiException as e:
            # How to write a crap REST API: require clients to sniff
            # HTTP status codes rather than provide a meaningful
            # exception heirarchy.
            if e.status != 409:
                raise

        service["metadata"]["name"] = self.standbys_service_name
        service["spec"]["selector"]["role"] = "standby"

        log.info(f"standbys Service definition <<EOM\n{yaml.dump(service)}\nEOM")
        try:
            api.create_namespaced_service(self.model.name, service)
        except kubernetes.client.rest.ApiException as e:
            # How to write a crap REST API: require clients to sniff
            # HTTP status codes rather than provide a meaningful
            # exception heirarchy.
            if e.status != 409:
                raise

    def get_k8s_service(self, name):
        self.k8s_auth()
        cl = kubernetes.client.ApiClient()
        api = kubernetes.client.CoreV1Api(cl)
        return api.read_namespaced_service(name, self.model.name)

    def on_db_relation_changed(self, event, admin=False):
        # Database username is the remote Application name.
        username = event.app.name

        # Password
        password = self.db_password(username)
        if not password:
            event.defer()  # Wait for leader to choose the password.
            return

        # Inspect requests from the client. First look in Application
        # data for modern clients. Fall back to eventually consistent
        # unit data.
        for bucket in [event.app, event.unit]:
            dbname = event.relation.data[bucket].get("database", "")
            sroles = event.relation.data[bucket].get("roles", "")
            roles = list(sroles.split(","))
            sextensions = event.relation.data[bucket].get("extensions", "")
            extensions = list(sextensions.split(","))
            if dbname or roles or extensions:
                break

        # Fall back to a database named after the remote Application.
        # This is problematic for cross-model relations, where the
        # remote Application name may have been anonymized and we
        # get a unique database name every fresh deployment.
        if not dbname:
            dbname = event.app.name

        # Publish allowed-subnets to the relation, listing the
        # egress-subnets that have been granted access.
        # TODO: Meaningless in the k8s PostgreSQL charm. Can we have the
        # k8s Service limit connections? Do we want to?
        allowed_subnets = self.get_allowed_subnets(event.relation)

        # Publish connection details to the master.
        master = ConnectionString(
            host=self.master_service_ip,
            dbname=dbname,
            port=5432,
            user=username,
            password=password,
            fallback_application_name=event.app.name,
            sslmode="prefer",
        )
        standbys = ConnectionString(
            host=self.standbys_service_ip,
            dbname=dbname,
            port=5432,
            user=username,
            password=password,
            fallback_application_name=event.app.name,
            sslmode="prefer",
        )

        # Echo back data to clients so they know their requested changes
        # have been made. On Application data for modern clients, and
        # unit data for backwards compatibility.
        to_publish = [event.relation.data[self.unit]]
        if self.unit.is_leader():
            to_publish.append(event.relation.data[self.app])
        for bucket in to_publish:
            # Workaround for https://github.com/canonical/operator/pull/399/files
            def bset(key, value):
                if value or key in bucket:
                    bucket[key] = value

            bset("database", dbname)
            bset("database", dbname)
            bset("roles", sroles)
            bset("extensions", sextensions)
            bset("allowed-subnets", allowed_subnets)
            bset("master", str(master))
            bset("standbys", str(standbys))

        self.ensure_user(username, password, admin=admin)
        self.ensure_db(dbname, username, roles, extensions)

    def on_db_admin_relation_changed(self, event):
        self.on_db_relation_changed(event, admin=True)

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

    def ensure_user(self, username, password, admin):
        log.critical("ensure_user not implemented")

    def ensure_db(self, dbname, ownername, roles, extensions):
        log.critical("ensure_db not implemented")


def _csplit(s) -> Iterable[str]:
    if s:
        for b in s.split(","):
            b = b.strip()
            if b:
                yield b
