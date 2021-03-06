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

import functools
import logging
import os
import os.path
import shutil
import subprocess
import sys
from textwrap import dedent
import time

import kubernetes
import psycopg2
from tenacity import before_log, retry, retry_if_exception_type, stop_after_delay, wait_random_exponential


PGDATA = os.environ["PGDATA"]  # No underscore, PostgreSQL config

PG_MAJOR = os.environ["PG_MAJOR"]
PG_CONF_DIR = "/srv/pgconf/12/main"
PG_HBA_CONF = os.path.join(PG_CONF_DIR, "pg_hba.conf")
PG_BIN = os.path.join("/usr/lib/postgresql", PG_MAJOR, "bin")
PG_STANDBY_SIGNAL = os.path.join(PGDATA, "standby.signal")  # Formerly recovery.conf

REPMGR_CONF = "/srv/pgconf/repmgr.conf"
REPMGR_LOG = "/var/log/postgresql/repmgr.log"

JUJU_POD_NAME = os.environ["JUJU_POD_NAME"]
JUJU_POD_NUMBER = int(JUJU_POD_NAME.split("-")[-1])
JUJU_NODE_NAME = os.environ["JUJU_NODE_NAME"]
JUJU_APPLICATION = os.environ["JUJU_APPLICATION"]
JUJU_UNIT_NAME = f"{JUJU_APPLICATION}/{JUJU_POD_NUMBER}"
JUJU_EXPECTED_UNITS = os.environ["JUJU_EXPECTED_UNITS"].split(" ")

NAMESPACE = os.environ["JUJU_POD_NAMESPACE"]
HOSTNAME = os.environ["HOSTNAME"]

AS_PG_CMD = ["sudo", "-u", "postgres", "-EH", "--"]
REPMGR_CMD = AS_PG_CMD + ["repmgr", "-f", REPMGR_CONF]

log = logging.getLogger(__name__)


class NoMasterException(Exception):
    """Exception raised by get_master when there is no master

    Code will normally retry until the master becomes available.
    """

    pass


# tenacity.retry decorator, retrying on PostgreSQL exceptions.
# Such as connection failures caused when attempting to connect
# to a database that isn't ready yet, and retrying on
# CalledProcessError, as returned by failed repmgr commands,
# and retrying on NoMasterException, when we need the master
# but it isn't available yet.
pgretry = functools.partial(
    retry,
    retry=retry_if_exception_type(psycopg2.OperationalError)
    | retry_if_exception_type(subprocess.CalledProcessError)
    | retry_if_exception_type(NoMasterException),
    stop=stop_after_delay(300),
    wait=wait_random_exponential(multiplier=1, max=20),
    reraise=True,
    before=before_log(log, logging.DEBUG),
)


def fix_mounts():
    log.info("Updating permissions and ownership of /srv")
    # Fix permissions on mounts and initialize with required dirs.
    shutil.chown("/srv", user="root", group="postgres")
    os.chmod("/srv", 0o775)

    log.info("Updating permissions and ownership of /var/log/postgresql")
    shutil.chown("/var/log/postgresql", user="root", group="postgres")
    os.chmod("/var/log/postgresql", 0o1775)

    for pgpath in ["/srv/pgdata", f"/srv/pgdata/{PG_MAJOR}", "/srv/pgconf"]:
        log.info(f"Updating permissions and ownership of {pgpath}")
        if not os.path.exists(pgpath):
            os.mkdir(pgpath, mode=0o775)
        shutil.chown(pgpath, user="postgres", group="postgres")


def db_exists() -> bool:
    return os.path.isdir(PGDATA)


def maybe_create_db() -> bool:
    if db_exists():
        log.info(f"PostgreSQL database cluster exists at {PGDATA}")
        return False

    initdb()  # This also creates config files.

    return True


@pgretry
def clone_master():
    # Determine the master inside this function rather than pass it,
    # so that if the retry decorator needs to retry it we catch any
    # changes.
    master = get_master()
    master_hostname = get_pod_hostname(master)
    log.info(f"Cloning database from {master} ({master_hostname})")
    shutil.rmtree(PGDATA)
    cmd = REPMGR_CMD + ["-h", master_hostname, "-U", "repmgr", "-d", "repmgr", "standby", "clone", "-c"]
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
        "--",
        "--auth-local=trust",
        "--auth-host=scram-sha-256",
    ]
    log.info(f"Running {' '.join(cmd)}")
    subprocess.run(cmd, check=True, text=True)


def start_db():
    log.info("Starting PostgreSQL cluster")
    subprocess.run(["pg_ctlcluster", PG_MAJOR, "main", "start"], check=True, text=True)


def update_postgresql_conf():
    pgconf_override = os.path.join(PG_CONF_DIR, "conf.d", "juju_charm.conf")
    log.info(f"Updating PostgreSQL configuration in {pgconf_override}")
    with open(pgconf_override, "w") as outf:
        outf.write(
            dedent(
                f"""\
                # This file is maintained by the Juju PostgreSQL k8s charm
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
    marker = "# These rules are appended by Juju"
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
                    # This file is maintained by the Juju PostgreSQL k8s charm
                    *:*:repmgr:repmgr:{pw}
                    *:*:replication:repmgr:{pw}
                    """
                )
            )
        os.chmod(pgpass, 0o600)
    shutil.chown(pg, user="postgres", group="postgres")


def update_repmgr_conf():
    log.info(f"Updating repmgr configuration in {REPMGR_CONF}")
    hostname = get_pod_hostname(JUJU_POD_NAME)
    with open(REPMGR_CONF, "w") as outf:
        outf.write(
            dedent(
                f"""\
                # This file maintained by the Juju PostgreSQL k8s charm

                node_id={JUJU_POD_NUMBER + 1}
                node_name='{JUJU_POD_NAME}'
                data_directory='{PGDATA}'

                pg_bindir='{PG_BIN}'
                repmgr_bindir='{PG_BIN}'

                log_level='INFO'
                log_facility='STDERR'
                log_file='{REPMGR_LOG}'  # TODO: Rotate this
                log_status_interval=300

                # Secret pulled from ~/.pgpass
                conninfo='host={hostname} user=repmgr dbname=repmgr connect_timeout=2'

                service_start_command   = 'pg_ctlcluster {PG_MAJOR} main start'
                service_stop_command    = 'pg_ctlcluster {PG_MAJOR} main stop'
                service_restart_command = 'pg_ctlcluster {PG_MAJOR} main restart'
                service_reload_command  = 'pg_ctlcluster {PG_MAJOR} main reload'
                service_promote_command = 'pg_ctlcluster {PG_MAJOR} main promote'

                # We do not set a location. We would need 2 nodes (or
                # one node + one witness) in each location or failover
                # will not occur.
                # location='{JUJU_NODE_NAME}'

                primary_visibility_consensus=true
                standby_disconnect_on_failover=true
                standby_reconnect_timeout=180
                node_rejoin_timeout=180

                failover=automatic
                promote_command='/usr/local/bin/repmgr_promote_command.py'
                follow_command='/usr/local/bin/repmgr_follow_command.py %n'

                # TODO: Schedule 'repmgr cluster cleanup'
                monitoring_history=yes
                """
            )
        )
    os.chmod(REPMGR_CONF, 0o644)


# Retry in case local PostgreSQL is still starting up.
@pgretry
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


# Retry in case DNS resolution is slow, waiting for the appname-master
# service to resolve to the local container.
@pgretry
def register_repmgr_master():
    log.info(f"Registering PostgreSQL primary server with repmgr")
    # Always reregister with force, as our IP address might have changed.
    cmd = REPMGR_CMD + ["primary", "register", "--force"]
    log.info(f"Running {' '.join(cmd)}")
    subprocess.run(cmd, check=True, text=True)


# Retry in case the master is not running, or gets shut down midway.
@pgretry
def register_repmgr_standby():
    master = get_master()
    log.info(f"Registering PostgreSQL hot standby server with {master}")
    # Always reregister with force, as our IP address might have changed.
    cmd = REPMGR_CMD + [
        "standby",
        "register",
        "--force",
        "--wait-sync=60",
        "-h",
        get_pod_hostname(master),
        "-U",
        "repmgr",
        "-d",
        "repmgr",
        "--verbose",  # TODO: Turn off verbosity?
        "--log-level=DEBUG",  # TODO: Turn off debug?
    ]
    log.info(f"Running {' '.join(cmd)}")
    subprocess.run(cmd, check=True, text=True)


def reconnect_repmgr_standby():
    if os.path.exists(PG_STANDBY_SIGNAL):
        # This node was a standby. It needs to be instructed to follow
        # the current master, in case that changed while it was down.
        # It was previously registered.
        follow_master()
    else:
        # This node was a master, but has been deposed. It needs
        # to do the 'repmgr rejoin' dance, possibly getting rewound
        # in the process. Ideally, we would rejoin standbys too
        # in case there were timeline changes while it was down,
        # but that does not seem to be working with repmgr 5.0 and
        # PostgreSQL 12.
        # TODO: Chase standby rejoin process bug.
        rejoin_master()


# Retry in case the master is not running.
@pgretry
def follow_master():
    master = get_master()
    log.info(f"Hot standby following {master}")
    master_hostname = get_pod_hostname(master)
    assert os.path.exists(PG_STANDBY_SIGNAL)
    start_db()
    cmd = REPMGR_CMD + ["-h", master_hostname, "-U", "repmgr", "-d", "repmgr", "standby", "follow"]
    log.info(f"Running {' '.join(cmd)}")
    subprocess.run(cmd, check=True, text=True)


@pgretry
def rejoin_master():
    master = get_master()
    log.info(f"Deposed master rejoining, following {master}")
    master_hostname = get_pod_hostname(master)

    # This node was a crashed master, and has since been deposed.
    # It needs to rejoin as a hot standby.
    ensure_consistent_db()

    cmd = REPMGR_CMD + ["-h", master_hostname, "-U", "repmgr", "-d", "repmgr", "node", "rejoin", "--force-rewind"]
    log.info(f"Running {' '.join(cmd)}")
    r = subprocess.run(cmd, text=True)
    # The 'rejoin' return codes documented, so use them.
    if r.returncode == 0:
        log.info(f"PostgreSQL hot standby rejoined {master}")
        return
    elif r.returncode == 1:
        raise RuntimeError("Bad repmgr configuration")  # Should not happen
    elif r.returncode == 4:
        raise RuntimeError("PostgreSQL could not be restarted by repmgr")  # TODO: DB corrupt? Maybe reclone?
    elif r.returncode == 24:
        raise RuntimeError("The repmgr rejoin operation failed")
    raise RuntimeError("The repmgr rejoin operation failed with an unknown error")


def ensure_consistent_db():
    log.info("Ensuring database is in a consistent state")
    has_signal = os.path.exists(PG_STANDBY_SIGNAL)
    if has_signal:
        os.unlink(PG_STANDBY_SIGNAL)
    else:
        log.warning("PostgreSQL is not following. Deposed master?")
    # -D $PG_CONF_DIR because we are using Debian layout (not -D $PGDATA).
    cmd = AS_PG_CMD + [os.path.join(PG_BIN, "postgres"), "--single", "-D", PG_CONF_DIR]
    log.info(f"Running {' '.join(cmd)}")
    subprocess.run(cmd, check=True, text=True, stdin=subprocess.DEVNULL)


def configure_k8s_api():
    kubernetes.config.load_incluster_config()


def is_master() -> bool:
    try:
        return get_master() == JUJU_POD_NAME
    except NoMasterException:
        return False


def get_master() -> str:
    cl = kubernetes.client.ApiClient()
    api = kubernetes.client.CoreV1Api(cl)
    master_selector = f"juju-app={JUJU_APPLICATION},role=master"
    masters = [i.metadata.name for i in api.list_namespaced_pod(NAMESPACE, label_selector=master_selector).items]
    if len(masters) == 1:
        return masters[0]
    elif len(masters) == 0 and JUJU_UNIT_NAME == JUJU_EXPECTED_UNITS[0]:
        # If there is no master, and this is the first of of the
        # expected pods, promote ourselves to master.
        return JUJU_POD_NAME
    elif len(masters) > 1:
        # Log a message, since this should never happen. Or perhaps it
        # does if label removal takes time to propogate between k8s
        # API servers.
        log.critical("Multiple PostgreSQL masters found. Too many pods with role=master label.")

    raise NoMasterException()


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


def set_standby():
    log.info("Labeling this pod as standby")
    cl = kubernetes.client.ApiClient()
    api = kubernetes.client.CoreV1Api(cl)
    api.patch_namespaced_pod(JUJU_POD_NAME, NAMESPACE, {"metadata": {"labels": {"role": "standby"}}})


def set_pod_label():
    key = "pgcharm-pod"
    value = JUJU_POD_NAME
    log.info(f"Labeling this pod as {key}={value} for service discovery")
    cl = kubernetes.client.ApiClient()
    api = kubernetes.client.CoreV1Api(cl)
    api.patch_namespaced_pod(JUJU_POD_NAME, NAMESPACE, {"metadata": {"labels": {key: value}}})


def get_pod_hostname(name) -> str:
    return f"{JUJU_APPLICATION}-{name}"


def init_logging():
    logging.basicConfig(format="%(asctime)-15s %(levelname)8s: %(message)s")
    log.setLevel(logging.DEBUG)


def debug_docker_entrypoint():
    import time
    import traceback

    try:
        docker_entrypoint()
    except Exception:
        traceback.print_exc()
        while True:
            time.sleep(600)


def docker_entrypoint():
    init_logging()
    configure_k8s_api()

    set_pod_label()  # Label pod to match the pod-unique Service selector.

    # Repmgr is configured to log to a file, because the history might
    # be needed for disaster recovery. But it is also useful for output
    # to be seen in the pod logs (?). So tail the repmgr log file.
    p = subprocess.Popen(["tail", "-F", REPMGR_LOG], text=True)

    # TODO: Schedule log rotations, PostgreSQL and repmgr

    fix_mounts()

    update_pgpass()

    update_repmgr_conf()

    db_created = maybe_create_db()

    update_postgresql_conf()

    if is_master():
        start_db()
        update_repmgr_db()
        register_repmgr_master()
        # Now DB is setup, advertise master status. This triggers other
        # pods to continue.
        set_master()

    elif db_created:
        # Hot standby that was just created. Clone the master, start,
        # and register.
        clone_master()
        start_db()
        register_repmgr_standby()
        set_standby()

    else:
        # Not the master, but database already exists. Either a
        # a hot standby or a deposed master. Rejoin the cluster
        # as a hot standby.
        reconnect_repmgr_standby()  # DB must not be running.
        set_standby()

    exec_repmgrd()  # Does not return

    p.terminate()


def exec_repmgrd():
    cmd = AS_PG_CMD + ["repmgrd", "-v", "-f", REPMGR_CONF, "--daemonize=false", "--no-pid-file"]
    os.execvp(cmd[0], cmd)  # Should not return


def promote_entrypoint():
    init_logging()
    log.warning("Promoting to master")
    configure_k8s_api()
    set_master()  # First, lessening chance connections go to an existing master.
    cmd = AS_PG_CMD + ["repmgr", "standby", "promote", "-v", "-f", REPMGR_CONF, "--log-to-file"]
    log.info(f"Running {' '.join(cmd)}")
    subprocess.check_call(cmd, text=True)


def follow_entrypoint():
    init_logging()
    node_id = int(sys.argv[1])
    log.warning(f"Following repmgr node {node_id}")
    configure_k8s_api()
    subprocess.check_call(
        AS_PG_CMD
        + [
            "repmgr",
            "standby",
            "follow",
            "-v",
            "--wait",
            "-f",
            REPMGR_CONF,
            "--log-to-file",
            f"--upstream-node-id={node_id}",
        ],
        text=True,
    )
    set_standby()


def hang_forever():
    while True:
        log.debug("Idling")
        time.sleep(600)
