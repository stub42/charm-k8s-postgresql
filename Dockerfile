# Copyright 2020 Canonical Ltd.
# Licensed under the GPLv3, see LICENCE file for details.

ARG DIST_RELEASE=focal

FROM golang:1.14 AS gobuilder
WORKDIR /go
RUN go get -v k8s.io/kubernetes/cmd/kubectl

FROM ubuntu:${DIST_RELEASE}

LABEL maintainer="postgresql-charmers@lists.launchpad.net"
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
EXPOSE 5432/tcp

COPY --from=gobuilder /go/bin/kubectl /usr/local/bin/
RUN chmod 0755 /usr/local/bin/kubectl

RUN \
# Avoid interactive prompts.
    echo 'debconf debconf/frontend select Noninteractive' | debconf-set-selections && \
# Update package database, remove cruft.
    apt-get update && apt-get --purge autoremove -y && \
# Create the en_US.UTF-8 locale before package installation, so
# databases will be UTF-8 enabled by default.
    apt-get install -y --no-install-recommends locales && \
    locale-gen en_US.UTF-8 && \
# Create postgres user with explicit user and group IDs.
    groupadd -r postgres --gid=999 && \
    useradd -r -g postgres --uid=999 --home-dir=/var/lib/postgresql --shell=/bin/bash postgres && \
# Ensure configuration is stored on persistent disk along with its
# corresponding database.
    mkdir -p /srv/pgconf && \
    ln -s /srv/pgconf /etc/postgresql

# Ensure pg_createcluster works the way we need, disable initial cluster
# creation. NB. .conf extension is required.
COPY ./files/createcluster.conf /etc/postgresql-common/createcluster.d/pgcharm.conf

ARG PG_MAJOR=12

# The PGDATA environment variable must match the data_directory setting
# in ./files/createcluster.conf.
ENV PGDATA="/srv/pgdata/${PG_MAJOR}/main" \
    LANG="en_US.UTF-8" \
    PATH="$PATH:/usr/lib/postgresql/${PG_MAJOR}/bin"

ARG PKGS_TO_INSTALL="postgresql postgresql-${PG_MAJOR}-repack repmgr"

RUN \
# Install remaining packages
    apt-get install -y --no-install-recommends ${PKGS_TO_INSTALL} && \
# Purge apt cache
    rm -rf /var/lib/apt/lists/*

# apt installation created and populated things, so now declare
# necessary persistent volumes.
VOLUME ["/srv", "/var/log/postgresql"]

COPY ./files/docker-entrypoint.sh /usr/local/bin/
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh

COPY ./files/docker-readyness.sh /usr/local/bin/
RUN chmod 0755 /usr/local/bin/docker-readyness.sh

# BUILD_DATE has a default set due to
# https://bugs.launchpad.net/launchpad/+bug/1892351.
ARG BUILD_DATE=unset
LABEL org.label-schema.build-date=${BUILD_DATE}
