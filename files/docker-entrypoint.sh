#!/bin/sh -ex

# Repair Juju storage mounts, per https://bugs.launchpad.net/juju/+bug/1892988
chown root:postgres /var/log/postgresql
chmod 1775 /var/log/postgresql
if [ ! -d /srv/pgdata ]; then mkdir /srv/pgdata; fi
if [ ! -d /srv/pgconf ]; then mkdir /srv/pgconf; fi

chown -R postgres:postgres /var/run/postgresql
chmod 2775 /var/run/postgresql

if [ -z "$PGDATA" ]; then
    echo PGDATA is not set
    exit 1
fi

# TODO: Only create database on the master
if [ ! -d "$PGDATA" ]; then
    mkdir -p "$PGDATA"
    chown postgres:postgres "$PGDATA"
    chmod 0750 "$PGDATA"
    pg_createcluster $(PG_MAJOR) main --locale=en_US.UTF-8 --port=5432 --datadir="$PGDATA"
fi

cat<<EOM > /etc/postgresql/$(PG_MAJOR)/main/conf.d/juju_charm.conf
# This file maintained by the PostgreSQL k8s Juju Charm

# TODO: Charm option to specify PostgreSQL configuration. Confirm
# behavior when settings duplicated in main postgresql.conf.

hot_standby = on
wal_level = replica  # TODO: logical replication?
max_wal_senders = 10  # TODO: number of nodes + 2 (repmgr) + slack
wal_log_hints = on  # Ignored; DB initialized with data checksums

wal_keep_segments = 500  # TODO: WAL archiving needed for real deployments
archive_mode = on
archive_command = '/bin/true'

EOM

# TODO: Create repmgr admin account if necessary.

# TODO: Reset repmgr admin password, as we might be mounting a recovered
# DB from a previous deployment and the secret has changed.

# TODO: On primary, 'ALTER EXTENSION repmgr UPDATE' in case repmgr has
# had a major upgrade.

tail -F /dev/null
