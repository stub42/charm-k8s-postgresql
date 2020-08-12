ARG DIST_RELEASE
FROM ubuntu:${DIST_RELEASE}

LABEL maintainer="postgresql-charmers@lists.launchpad.net"

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

EXPOSE 5432/tcp
EXPOSE 22/tcp

ARG PG_VER
ARG PKGS_TO_INSTALL

ENV DATADIR=/var/lib/postgresql/${PG_VER}/main

# Avoid interactive prompts
RUN echo 'debconf debconf/frontend select Noninteractive' | debconf-set-selections

# Update all packages, remove cruft, install required packages
RUN apt-get update && apt-get -y dist-upgrade \
    && apt-get --purge autoremove -y \
    && apt-get install -y ${PKGS_TO_INSTALL}

COPY ./files/docker-entrypoint.sh /usr/local/bin/
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh

COPY ./files/docker-readyness.sh /usr/local/bin/
RUN chmod 0755 /usr/local/bin/docker-readyness.sh

ARG BUILD_DATE
LABEL org.label-schema.build-date=${BUILD_DATE}
