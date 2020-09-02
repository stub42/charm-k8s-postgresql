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

import logging
import os
import os.path
import shutil
import subprocess
from textwrap import dedent
import time
import traceback

import kubernetes
import psycopg2


PGDATA = os.environ["PGDATA"]  # No underscore, PostgreSQL config

PG_MAJOR = os.environ["PG_MAJOR"]
PG_CONF_DIR = "/srv/pgconf/12/main"
PG_HBA_CONF = os.path.join(PG_CONF_DIR, "pg_hba.conf")
REPMGR_CONF = "/srv/pgconf/repmgr.conf"

JUJU_POD_NAME = os.environ["JUJU_POD_NAME"]
JUJU_POD_NUMBER = int(JUJU_POD_NAME.split("-")[-1])
JUJU_APPLICATION = os.environ["JUJU_APPLICATION"]
JUJU_UNIT_NAME = f"{JUJU_APPLICATION}/{JUJU_POD_NUMBER}"
JUJU_EXPECTED_UNITS = os.environ["JUJU_EXPECTED_UNITS"].split(" ")

NAMESPACE = os.environ["JUJU_POD_NAMESPACE"]
HOSTNAME = os.environ["HOSTNAME"]

REPMGR_CMD = ["sudo", "-u", "postgres", "-H", "--", "repmgr", "-f", REPMGR_CONF]


log = logging.getLogger("docker-entrypoint")


def fix_mounts():
    log.info("Updating permissions and ownership of /srv")
    # Fix permissions on mounts and initialize with required dirs.
    shutil.chown("/srv", user="root", group="postgres")
    os.chmod("/srv", 0o775)

    # TODO: log mount not wired up correctly? Isn't appearing in mounts.
    log.info("Updating permissions and ownership of /var/log/postgresql")
    shutil.chown("/var/log/postgresql", user="root", group="postgres")
    os.chmod("/var/log/postgresql", 0o1775)

    for pgpath in ["/srv/pgdata", f"/srv/pgdata/{PG_MAJOR}", "/srv/pgconf"]:
        log.info(f"Updating permissions and ownership of {pgpath}")
        if not os.path.exists(pgpath):
            os.mkdir(pgpath, mode=0o775)
        shutil.chown(pgpath, user="postgres", group="postgres")


def db_exists():
    return os.path.isdir(PGDATA)


def maybe_create_db() -> bool:
    if db_exists():
        log.info(f"PostgreSQL database cluster exists at {PGDATA}")
        return False

    log.info("Checking pod labels for master")
    master = wait_master()
    log.info(f"{master} is the master")
    if master == JUJU_POD_NAME:
        log.info("Hey, that's me!")

    initdb()  # This also creates config files.

    if master != JUJU_POD_NAME:
        clone_master(master)

    return True


def clone_master(master):
    master_ip = get_pod_ip(master)
    log.info(f"Cloning database from {master} ({master_ip})")
    shutil.rmtree(PGDATA)
    cmd = REPMGR_CMD + ["-h", master_ip, "-U", "repmgr", "-d", "repmgr", "standby", "clone"]
    log.info(f"Running {' '.join(cmd)}")
    subprocess.run(cmd, check=True, text=True)


def show_repmgr_cluster():
    cmd = REPMGR_CMD + ["cluster", "show"]
    log.info(f"Running {' '.join(cmd)}")
    subprocess.run(cmd, check=True, text=True)


def initdb():
    log.warning(f"Creating new database cluster in {PGDATA}")
    os.makedirs(PGDATA, mode=0o755)  # mode for intermediate directories
    shutil.chown(PGDATA, user="postgres", group="postgres")
    os.chmod(PGDATA, 0o700)  # Required mode for $PGDATA
    cmd = [
        "pg_createcluster",
        PG_MAJOR,
        "main",
        "--locale=en_US.UTF-8",
        "--port=5432",
        "--datadir=" + PGDATA,
        "--auth-local=trust",
        "--auth-host=scram-sha-256",
    ]
    log.info(f"Running {' '.join(cmd)}")
    subprocess.run(
        ["pg_createcluster", PG_MAJOR, "main", "--locale=en_US.UTF-8", "--port=5432", "--datadir=" + PGDATA],
        check=True,
        text=True,
    )


def start_db():
    log.info("Starting PostgreSQL cluster")
    # TODO: Use pg_ctlcluster? Or pg_ctl like repmgr default?
    subprocess.run(["pg_ctlcluster", PG_MAJOR, "main", "start"], check=True, text=True)


def update_postgresql_conf():
    pgconf_override = os.path.join(PG_CONF_DIR, "conf.d", "juju_charm.conf")
    log.info(f"Updating PostgreSQL configuration in {pgconf_override}")
    with open(pgconf_override, "w") as outf:
        outf.write(
            dedent(
                f"""\
                # This file maintained by the Juju PostgreSQL k8s charm
                listen_addresses = '*'
                hot_standby = on
                wal_level = replica
                max_wal_senders = {len(JUJU_EXPECTED_UNITS) + 2 + 2}  # num units + 2 (repmgr) + slack
                wal_log_hints = on  # Ignored due to data checksums, but just in case
                wal_keep_segments = 500  # TODO: WAL archiving needed for real deployments
                archive_mode = on
                archive_command = '/bin/true'

                shared_preload_libraries = 'repmgr'  # Required for using repmgrd
                """
            )
        )
    os.chmod(pgconf_override, 0o644)

    hba = open(PG_HBA_CONF, "r").readlines()
    marker = "# These rules appended by Juju"
    if (marker + "\n") not in hba:
        with open(PG_HBA_CONF, "a") as outf:
            outf.write("\n")
            outf.write(
                dedent(
                    f"""\
                    {marker}
                    # TODO: Can we restrict them to just the pod IPs?
                    host all         all 0.0.0.0/0 scram-sha-256
                    host all         all ::0/0     scram-sha-256
                    host replication all 0.0.0.0/0 scram-sha-256
                    host replication all ::0/0     scram-sha-256
                    """
                )
            )


def get_pgsql_admin_password():
    return open("/charm-secrets/pgsql-admin-password", "r").read().strip()


def update_pgpass():
    root = os.path.expanduser("~root/.pgpass")
    pg = os.path.expanduser("~postgres/.pgpass")
    pw = get_pgsql_admin_password()
    for pgpass in [root, pg]:
        log.info(f"Overwriting {pgpass}, updating secrets")
        with open(pgpass, "w") as outf:
            outf.write(
                dedent(
                    f"""\
                    # This file is maintained by Juju
                    *:*:repmgr:repmgr:{pw}
                    *:*:replication:repmgr:{pw}
                    """
                )
            )
        os.chmod(pgpass, 0o600)
    shutil.chown(pg, user="postgres", group="postgres")


def update_repmgr_conf():
    log.info(f"Updating repmgr configuration in {REPMGR_CONF}")
    ip = get_pod_ip(JUJU_POD_NAME)
    with open(REPMGR_CONF, "w") as outf:
        outf.write(
            dedent(
                f"""\
                # This file maintained by the Juju PostgreSQL k8s charm

                node_id={JUJU_POD_NUMBER + 1}
                node_name='{JUJU_POD_NAME}'
                data_directory='{PGDATA}'

                pg_bindir='/usr/lib/postgresql/{PG_MAJOR}/bin'
                repmgr_bindir='/usr/lib/postgresql/{PG_MAJOR}/bin'

                log_level='INFO'
                log_facility='STDERR'
                log_file='/var/log/postgresql/repmgr.log'  # TODO: Rotate this
                log_status_interval=300

                pg_basebackup_options='--wal-method=stream --checkpoint=fast'

                # Secret pulled from ~/.pgpass
                conninfo='host={ip} user=repmgr dbname=repmgr connect_timeout=2'

                # TODO: Ensure repmgr_standby_clone uses --fast-checkpooint
                """
            )
        )
    os.chmod(REPMGR_CONF, 0o644)


def update_repmgr_db():
    log.info(f"Resetting repmgr database user password")
    con = psycopg2.connect("dbname=postgres user=postgres")
    con.autocommit = True
    cur = con.cursor()
    pw = get_pgsql_admin_password()

    cur.execute("SELECT TRUE FROM pg_roles WHERE rolname='repmgr'")
    exists = cur.fetchone() is not None

    cmd = "ALTER" if exists else "CREATE"
    cur.execute(f"{cmd} ROLE repmgr WITH LOGIN SUPERUSER REPLICATION PASSWORD %s", (pw,))

    log.info(f"Maintaining repmgr database")
    cur.execute("SELECT TRUE FROM pg_database WHERE datname='repmgr'")
    exists = cur.fetchone() is not None
    if exists:
        cur.execute("ALTER DATABASE repmgr OWNER TO repmgr")
    else:
        cur.execute("CREATE DATABASE repmgr OWNER repmgr")
        register_repmgr_master()


def register_repmgr_master():
    log.info(f"Registering PostgreSQL primary server with repmgr")
    cmd = REPMGR_CMD + ["primary", "register"]
    log.info(f"Running {' '.join(cmd)}")
    subprocess.run(cmd, check=True, text=True)


def configure_k8s_api():
    kubernetes.config.load_incluster_config()


def is_master() -> bool:
    return get_master() == JUJU_POD_NAME


def get_master() -> str:
    cl = kubernetes.client.ApiClient()
    api = kubernetes.client.CoreV1Api(cl)
    master_selector = f"juju-app={JUJU_APPLICATION},role=master"
    masters = [i.metadata.name for i in api.list_namespaced_pod(NAMESPACE, label_selector=master_selector).items]
    if len(masters) == 1:
        return masters[0]
    elif len(masters) > 1:
        log.critical("Multiple PostgreSQL masters found. Too many pods with role=master label.")
        # Code already needs to cope with no-master-yet, so we don't
        # have to fail hard.
        return None

    # If there is no master, and this is the first of of the
    # expected pods, promote ourselves to master.
    if JUJU_UNIT_NAME == JUJU_EXPECTED_UNITS[0]:
        return JUJU_POD_NAME

    return None


def wait_master() -> str:
    while True:
        master = get_master()
        if master:
            return master
        time.sleep(5)


def set_master():
    log.info("Labeling this pod as master")
    cl = kubernetes.client.ApiClient()
    api = kubernetes.client.CoreV1Api(cl)
    master_selector = f"juju-app={JUJU_APPLICATION},role=master"
    masters = [i.metadata.name for i in api.list_namespaced_pod(NAMESPACE, label_selector=master_selector).items]
    found = False
    for master in masters:
        if master == JUJU_UNIT_NAME:
            found = True
        else:
            api.patch_namespaced_pod(master, NAMESPACE, {"metadata": {"labels": {"role": None}}})
    if not found:
        api.patch_namespaced_pod(JUJU_POD_NAME, NAMESPACE, {"metadata": {"labels": {"role": "master"}}})


def maybe_set_master():
    if is_master():
        set_master()


def set_standby():
    cl = kubernetes.client.ApiClient()
    api = kubernetes.client.CoreV1Api(cl)
    api.patch_namespaced_pod(JUJU_POD_NAME, NAMESPACE, {"metadata": {"labels": {"role": "standby"}}})


def get_pod_ip(name) -> str:
    cl = kubernetes.client.ApiClient()
    api = kubernetes.client.CoreV1Api(cl)
    pod = api.read_namespaced_pod(name, NAMESPACE)
    return pod.status.pod_ip


def main():
    logging.basicConfig(format="%(asctime)-15s %(levelname)8s: %(message)s")
    log.setLevel(logging.DEBUG)

    log.info(f"Running {__file__}")

    if not PGDATA:
        log.critical("$PGDATA environment variable is not set.")
        raise SystemExit(1)

    configure_k8s_api()

    fix_mounts()

    update_pgpass()

    update_repmgr_conf()  # First, because repmgr will be used to clone.

    maybe_create_db()

    update_postgresql_conf()

    start_db()  # TODO: Ensure DB shutdown cleanly

    if is_master():
        update_repmgr_db()

    # Last, to ensure new master setup before standbys attempt to clone.
    maybe_set_master()


def hang_forever():
    while True:
        log.debug("Idling")
        time.sleep(600)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()  # TODO: Stop this, pod should fail.
    hang_forever()
