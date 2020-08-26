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
    pg_createcluster 12 main --locale=en_US.UTF-8 --port=5432 --datadir="$PGDATA"
fi

# TODO: Reset admin password, as we might be mounting a recovered DB
# from a previous deployment and the secret has changed.

tail -F /dev/null
