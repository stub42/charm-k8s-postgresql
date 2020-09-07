#!/usr/bin/env python3

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

from base64 import b64encode
import logging
import os
from pathlib import Path
import subprocess
from typing import Dict, Iterable, List

from charmhelpers.core import host, hookenv
import kubernetes
import ops.charm
import ops.main
import ops.model
import yaml

logger = logging.getLogger(__name__)

REQUIRED_SETTINGS = ["image"]


class PostgreSQLCharm(ops.charm.CharmBase):
    def __init__(self, *args):
        super().__init__(*args)

        self.framework.observe(self.on.start, self.on_config_changed)
        self.framework.observe(self.on.leader_elected, self.on_config_changed)
        self.framework.observe(self.on.config_changed, self.on_config_changed)
        self.framework.observe(self.on.upgrade_charm, self.on_config_changed)
        self.framework.observe(self.on["peer"].relation_joined, self.on_config_changed)
        self.framework.observe(self.on["peer"].relation_departed, self.on_config_changed)

        self.framework.observe(self.on.start, self.create_k8s_service)

    def _check_for_config_problems(self) -> str:
        """Return config related problems as a human readable string."""
        problems = []

        missing = self._missing_charm_settings()
        if missing:
            problems.append("required setting(s) empty: {}".format(", ".join(sorted(missing))))

        return "; ".join(filter(None, problems))

    def _missing_charm_settings(self) -> Iterable[str]:
        """Return a list of required configuration settings that are not set."""
        config = self.model.config
        missing = [setting for setting in REQUIRED_SETTINGS if not config[setting]]
        if config["image_username"] and not config["image_password"]:
            missing.append("image_password")
        return sorted(missing)

    def on_config_changed(self, event: ops.charm.ConfigChangedEvent):
        """Check that we're leader, and if so, set up the pod."""
        if self.model.unit.is_leader():

            goal_state = hookenv.goal_state()

            logger.info("Goal state <<EOM\n{}\nEOM".format(yaml.dump(goal_state)))

            # Only the leader can set_spec().
            spec = self.make_pod_spec()
            resources = self.make_pod_resources()

            msg = "Configuring pod"
            logger.info(msg)
            self.model.unit.status = ops.model.MaintenanceStatus(msg)

            self.model.pod.set_spec(spec, {"kubernetesResources": resources})

            msg = "Pod configured"
            logger.info(msg)
            self.model.unit.status = ops.model.ActiveStatus(msg)
        else:
            logger.info("Spec changes ignored by non-leader")
            self.model.unit.status = ops.model.ActiveStatus()

    def make_pod_spec(self) -> Dict:
        """Set up and return our full pod spec here."""
        config = self.model.config

        image_details = {
            "imagePath": config["image"],
        }

        ports = [
            {"name": "pgsql", "containerPort": 5432, "protocol": "TCP"},
        ]

        config_fields = {
            "JUJU_NODE_NAME": "spec.nodeName",
            "JUJU_POD_NAME": "metadata.name",
            "JUJU_POD_NAMESPACE": "metadata.namespace",
            "JUJU_POD_IP": "status.podIP",
            "JUJU_POD_SERVICE_ACCOUNT": "spec.serviceAccountName",
        }
        env_config = {k: {"field": {"path": p, "api-version": "v1"}} for k, p in config_fields.items()}

        env_config["JUJU_EXPECTED_UNITS"] = " ".join(self.expected_units)
        env_config["JUJU_APPLICATION"] = self.app.name

        vol_config = [
            {"name": "charm-secrets", "mountPath": "/charm-secrets", "secret": {"name": "charm-secrets"}},
            {"name": "var-run-postgresql", "mountPath": "/var/run/postgresql", "emptyDir": {"medium": "Memory"}},
        ]

        spec = {
            "version": 3,
            "containers": [
                {
                    "name": self.app.name,
                    "imageDetails": image_details,
                    "imagePullPolicy": "Always",  # TODO: Necessary? Should this be a Juju default?
                    "ports": ports,
                    "envConfig": env_config,
                    "volumeConfig": vol_config,
                    # "kubernetes": {"readinessProbe": {"exec": {"command": ["/usr/local/bin/docker-readyness.sh"]}}},
                    # "kubernetes": {"readinessProbe": {"tcpSocket":
                    #     {"port": 5432, "initialDelaySeconds": 10, "periodSeconds": 25}}},
                }
            ],
        }
        logger.info(f"Pod spec <<EOM\n{yaml.dump(spec)}\nEOM")

        # After logging, attach our secrets.
        if config.get("image_username"):
            image_details["username"] = config["image_username"]
        if config.get("image_password"):
            image_details["password"] = config["image_password"]

        return spec

    def make_pod_resources(self) -> Dict:
        """Compile and return our pod resources (e.g. ingresses)."""
        secrets_data = {}  # Fill dictionary with secrets after logging resources
        resources = {"secrets": [{"name": "charm-secrets", "type": "Opaque", "data": secrets_data}]}
        logger.info(f"Pod resources <<EOM\n{yaml.dump(resources)}\nEOM")

        secrets = {"pgsql-admin-password": self.get_admin_password()}
        for k, v in secrets.items():
            secrets_data[k] = b64encode(v.encode("UTF-8")).decode("UTF-8")

        return resources

    def get_admin_password(self) -> str:
        pw = _leader_get("admin_password")
        if not pw:
            pw = host.pwgen(40)
            _leader_set({"admin_password": pw})
        return pw

    @property
    def expected_units(self) -> List[str]:
        # Goal state looks like this:
        #
        # relations: {}
        # units:
        #   postgresql/0:
        #     since: '2020-08-31 11:05:32Z'
        #     status: active
        #   postgresql/1:
        #     since: '2020-08-31 11:05:54Z'
        #     status: maintenance
        return sorted(hookenv.goal_state().get("units", {}).keys(), key=lambda x: int(x.split("/")[-1]))

    _authed = False

    def k8s_auth(self):
        if not self._authed:
            # Per lp:1892255
            os.environ.update(
                dict(
                    e.split("=") for e in Path("/proc/1/environ").read_text().split("\x00") if "KUBERNETES_SERVICE" in e
                )
            )
            kubernetes.config.load_incluster_config()
            self._authed = True

    @property
    def master_service_name(self) -> str:
        return f"{self.app.name}-master"

    @property
    def standbys_service_name(self) -> str:
        return f"{self.app.name}-standby"

    def create_k8s_service(self, event) -> None:
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

        logger.info(f"master Service definition <<EOM\n{yaml.dump(service)}\nEOM")
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

        logger.info(f"standbys Service definition <<EOM\n{yaml.dump(service)}\nEOM")
        try:
            api.create_namespaced_service(self.model.name, service)
        except kubernetes.client.rest.ApiException as e:
            # How to write a crap REST API: require clients to sniff
            # HTTP status codes rather than provide a meaningful
            # exception heirarchy.
            if e.status != 409:
                raise

    def get_k8s_service(self, name):
        cl = kubernetes.client.ApiClient()
        api = kubernetes.client.CoreV1Api(cl)
        return api.read_namespaced_service(name, self.model.name)


def _leader_get(attribute: str) -> str:
    cmd = ["leader-get", "--format=yaml", attribute]
    return yaml.safe_load(subprocess.check_output(cmd).decode("UTF-8"))


def _leader_set(settings: Dict[str, str]):
    cmd = ["leader-set"] + ["{}={}".format(k, v or "") for k, v in settings.items()]
    subprocess.check_call(cmd)


if __name__ == "__main__":
    ops.main.main(PostgreSQLCharm, use_juju_for_storage=True)
