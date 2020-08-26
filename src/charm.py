#!/usr/bin/env python3

# Copyright 2020 Canonical Ltd.
# Licensed under the GPLv3, see LICENCE file for details.

from base64 import b64encode
import logging
import subprocess
from typing import Dict, Iterable

import charmhelpers.core.host as host
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
            # Only the leader can set_spec().
            spec = self.make_pod_spec()
            resources = self.make_pod_resources()

            msg = "Configuring pod"
            logger.info(msg)
            self.model.unit.status = ops.model.MaintenanceStatus(msg)

            # https://bugs.launchpad.net/juju/+bug/1880637
            # self.model.pod.set_spec(spec, {'kubernetesResources': resources})
            spec.update({"kubernetesResources": resources})
            self.model.pod.set_spec(spec)

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
        env_config["PGSQL_ADMIN_PASSWORD"] = {"secret": {"name": "charm-secrets", "key": "pgsql-admin-password"}}

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
                    "ports": ports,
                    "envConfig": env_config,
                    "volumeConfig": vol_config,
                    # "kubernetes": {"readinessProbe": {"exec": {"command": ["/usr/local/bin/docker-readyness.sh"]}}},
                    # "kubernetes": {"readinessProbe": {"tcpSocket":
                    #     {"port": 5432, "initialDelaySeconds": 10, "periodSeconds": 25}}},
                }
            ],
        }
        logger.info("Pod spec <<EOM\n{}\nEOM".format(yaml.dump(spec)))

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
        logger.info("Pod resources <<EOM\n{}\nEOM".format(yaml.dump(resources)))

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


def _leader_get(attribute: str) -> str:
    cmd = ["leader-get", "--format=yaml", attribute]
    return yaml.safe_load(subprocess.check_output(cmd).decode("UTF-8"))


def _leader_set(settings: Dict[str, str]):
    cmd = ["leader-set"] + ["{}={}".format(k, v or "") for k, v in settings.items()]
    subprocess.check_call(cmd)


if __name__ == "__main__":
    ops.main.main(PostgreSQLCharm, use_juju_for_storage=True)
