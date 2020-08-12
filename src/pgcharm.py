#!/usr/bin/env python3

# Copyright 2020 Canonical Ltd.
# Licensed under the GPLv3, see LICENCE file for details.

import io
import logging
from ops.charm import CharmBase
from ops.model import ActiveStatus, MaintenanceStatus
from pprint import pprint
from yaml import safe_load

logger = logging.getLogger(__name__)

REQUIRED_SETTINGS = ['image']


class PostgreSQLCharm(CharmBase):
    def __init__(self, *args):
        super().__init__(*args)
        self.framework.observe(self.on.start, self.on_config_changed)
        self.framework.observe(self.on.config_changed, self.on_config_changed)
        self.framework.observe(self.on.upgrade_charm, self.on_config_changed)

    def _check_for_config_problems(self):
        """Return config related problems as a human readable string."""
        problems = []

        missing = self._missing_charm_settings()
        if missing:
            problems.append('required setting(s) empty: {}'.format(', '.join(sorted(missing))))

        return '; '.join(filter(None, problems))

    def _missing_charm_settings(self):
        """Return a list of required configuration settings that are not set."""
        config = self.model.config
        missing = [setting for setting in REQUIRED_SETTINGS if not config[setting]]
        if config['image_username'] and not config['image_password']:
            missing.append('image_password')
        return sorted(missing)

    def on_config_changed(self, event):
        """Check that we're leader, and if so, set up the pod."""
        if self.model.unit.is_leader():
            # Only the leader can set_spec().
            resources = self.make_pod_resources()
            spec = self.make_pod_spec()
            spec.update(resources)

            msg = "Configuring pod"
            logger.info(msg)
            self.model.unit.status = MaintenanceStatus(msg)
            self.model.pod.set_spec(spec)

            msg = "Pod configured"
            logger.info(msg)
            self.model.unit.status = ActiveStatus(msg)
        else:
            logger.info("Spec changes ignored by non-leader")
            self.model.unit.status = ActiveStatus()

    def make_pod_resources(self):
        """Compile and return our pod resources (e.g. ingresses)."""
        # LP#1889746: We need to define a manual ingress here to work around LP#1889703.
        resources = {}  # TODO
        out = io.StringIO()
        pprint(resources, out)
        logger.info("This is the Kubernetes Pod resources <<EOM\n{}\nEOM".format(out.getvalue()))
        return resources

    def generate_pod_config(self, secured=True):
        """Kubernetes pod config generator.

        generate_pod_config generates Kubernetes deployment config.
        If the secured keyword is set then it will return a sanitised copy
        without exposing secrets.
        """
        config = self.model.config
        pod_config = {}
        if config["container_config"].strip():
            pod_config = safe_load(config["container_config"])

        if secured:
            return pod_config

        if config["container_secrets"].strip():
            container_secrets = safe_load(config["container_secrets"])
            pod_config.update(container_secrets)

        return pod_config

    def make_pod_spec(self):
        """Set up and return our full pod spec here."""
        config = self.model.config
        full_pod_config = self.generate_pod_config(secured=False)
        secure_pod_config = self.generate_pod_config(secured=True)

        ports = [
            {"name": "postgresql", "containerPort": 5432, "protocol": "TCP"},
            {"name": "ssh", "containerPort": 22, "protocol": "TCP"},
        ]

        spec = {
            "version": 3,
            "containers": [
                {
                    "name": self.app.name,
                    "imageDetails": {"imagePath": config["image"]},
                    "ports": ports,
                    "envConfig": secure_pod_config,
                    # "kubernetes": {"readinessProbe": {"exec": {"command": ["/usr/local/bin/docker-readyness.sh"]}}},
                    # "kubernetes": {"readinessProbe": {"tcpSocket":
                    #     {"port": 5432, "initialDelaySeconds": 10, "periodSeconds": 25}}},
                }
            ],
        }

        out = io.StringIO()
        pprint(spec, out)
        logger.info("This is the Kubernetes Pod spec config (sans secrets) <<EOM\n{}\nEOM".format(out.getvalue()))

        if config.get("image_username"):
            spec.get("containers")[0].get("imageDetails")["username"] = config["image_username"]
        if config.get("image_password"):
            spec.get("containers")[0].get("imageDetails")["password"] = config["image_password"]

        secure_pod_config.update(full_pod_config)

        return spec
