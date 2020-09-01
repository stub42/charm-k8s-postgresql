#!/usr/bin/python3

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

from contextlib import contextmanager
import logging
import os
import os.path
import shutil
import subprocess
from textwrap import dedent
import time

import kubernetes


PGDATA = os.environ["PGDATA"]  # No underscore, PostgreSQL config

PG_MAJOR = os.environ["PG_MAJOR"]
PG_CONF_DIR = "/srv/pgconf/12/main"
REPMGR_CONF = "/srv/pgconf/repmgr.conf"

JUJU_POD_NAME = os.environ["JUJU_POD_NAME"]
JUJU_POD_NUMBER = int(JUJU_POD_NAME.split("-")[-1])


log = logging.getLogger("docker-entrypoint")


def fix_mounts():
    log.info("Updating permissions and ownership of /srv")
    # Fix permissions on mounts and initialize with required dirs.
    shutil.chown("/srv", user="root", group="postgres")
    os.chmod("/srv", 0o775)

    log.info("Updating permissions and ownership of /var/log/postgresql")
    shutil.chown("/var/log/postgresql", user="root", group="postgres")
    os.chmod("/var/log/postgresql", 0o1775)

    for pgpath in ["/srv/pgdata", "/srv/pgconf"]:
        log.info(f"Updating permissions and ownership of {pgpath}")
        if not os.path.exists(pgpath):
            os.mkdir(pgpath, mode=0o775)
        shutil.chown(pgpath, user="postgres", group="postgres")


def create_db():
    if not os.path.isdir(PGDATA):
        log.warning(f"Creating new database cluster in {PGDATA}")
        os.makedirs(PGDATA, mode=0o755)  # mode for intermediate directories
        shutil.chown(PGDATA, user="postgres", group="postgres")
        os.chmod(PGDATA, 0o700)  # Required mode for $PGDATA
        # TODO: Only initialize database on the master
        cmd = ["pg_createcluster", PG_MAJOR, "main", "--locale=en_US.UTF-8", "--port=5432", "--datadir=" + PGDATA]
        log.info(f"Running {' '.join(cmd)}")
        subprocess.run(
            ["pg_createcluster", PG_MAJOR, "main", "--locale=en_US.UTF-8", "--port=5432", "--datadir=" + PGDATA],
            check=True,
            text=True,
        )


def update_postgresql_conf():
    pgconf_override = os.path.join(PG_CONF_DIR, "conf.d", "juju_charm.conf")
    log.info(f"Updating PostgreSQL configuration in {pgconf_override}")
    with open(pgconf_override, "w") as outf:
        outf.write(
            dedent(
                f"""\
                # This file maintained by the Juju PostgreSQL k8s charm
                hot_standby = on
                wal_level = replica  # TODO: logical replication?
                max_wal_senders = 10  # TODO: number of nodes + 2 (repmgr) + slack
                wal_log_hints = on  # Ignored due to data checksums, but just in case
                wal_keep_segments = 500  # TODO: WAL archiving needed for real deployments
                archive_mode = on
                archive_command = '/bin/true'

                shared_preload_libraries = 'repmgr'  # Required for using repmgrd
                """
            )
        )
    os.chmod(pgconf_override, 0o644)


def update_repmgr_conf():
    log.info(f"Updating repmgr configuration in {REPMGR_CONF}")
    with open(REPMGR_CONF, "w") as outf:
        outf.write(
            dedent(
                f"""\
                # This file maintained by the Juju PostgreSQL k8s charm

                node_id={JUJU_POD_NUMBER}
                node_name='{JUJU_POD_NAME}'
                data_directory='{PGDATA}'
                pg_bindir='/usr/lib/postgresql/{PG_MAJOR}/bin'
                repmgr_bindir='/usr/lib/postgresql/{PG_MAJOR}/bin'
                config_directory='{PG_CONF_DIR}'
                log_level='INFO'
                log_facility='STDERR'
                log_file='/var/log/postgresql/repmgr.log'  # TODO: Rotate this
                log_status_interval=300

                # TODO: valid conninfo might help. Store secrets in .pgpass
                # conninfo='host=node1 user=repmgr dbname=repmgr connect_timeout=2 passfile=....'

                # TODO: Ensure repmgr_standby_clone uses --fast-checkpooint
                """
            )
        )
    os.chmod(REPMGR_CONF, 0o644)


_k8s_client = None


@contextmanager
def k8s():
    try:
        global _k8s_client
        if _k8s_client is None:
            log.info("Connecting to k8s API")
            kubernetes.config.load_incluster_config()
            _k8s_client = kubernetes.client.CoreV1Api()
        yield _k8s_client
    finally:
        pass


def main():
    logging.basicConfig(format="%(asctime)-15s %(levelname)8s: %(message)s")
    log.setLevel(logging.DEBUG)

    log.info(f"Running {__file__}")

    if not PGDATA:
        log.critical("$PGDATA environment variable is not set.")
        raise SystemExit(1)

    fix_mounts()

    create_db()

    update_postgresql_conf()

    # TODO: Create repmgr admin account if necessary. Maybe use trust auth instead?
    # https://repmgr.org/docs/current/quickstart-repmgr-user-database.html

    # TODO: Reset repmgr admin password, as we might be mounting a recovered
    # DB from a previous deployment and the secret has changed.

    # TODO: On primary, 'ALTER EXTENSION repmgr UPDATE' in case repmgr has
    # had a major upgrade.

    # TODO: Update pg_hba.conf to allow replication and repmgr connections.

    # TODO: Register repmgr master node

    # TODO: Clone repmgr standby nodes

    # TODO: Register repmgr standby nodes

    while True:
        log.debug('Idling')
        time.sleep(600)


if __name__ == "__main__":
    main()
