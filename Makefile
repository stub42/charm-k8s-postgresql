DIST_RELEASE ?= focal
DOCKER_DEPS = postgresql repmgr postgresql-12-repack repmgr openssh-server unattended-upgrades

blacken:
	@echo "Normalising python layout with black."
	@tox -e black


lint: blacken
	@echo "Running flake8"
	@tox -e lint

# We actually use the build directory created by charmcraft,
# but the .charm file makes a much more convenient sentinel.
unittest: bind.charm
	@tox -e unit

test: lint unittest

clean:
	@echo "Cleaning files"
	@git clean -fXd

bind.charm: src/*.py requirements.txt
	charmcraft build

image-deps:
	@echo "Checking shellcheck is present."
	@command -v shellcheck >/dev/null || { echo "Please install shellcheck to continue ('sudo snap install shellcheck')" && false; }

image-lint: image-deps
	@echo "Running shellcheck."
	@shellcheck files/docker-entrypoint.sh
	@shellcheck files/dns-check.sh

image-build: image-lint
	@echo "Building the image."
	@docker build \
		--no-cache=true \
		--build-arg BUILD_DATE=$$(date -u +'%Y-%m-%dT%H:%M:%SZ') \
		--build-arg PKGS_TO_INSTALL='$(DOCKER_DEPS)' \
		--build-arg DIST_RELEASE=$(DIST_RELEASE) \
		-t bind:$(DIST_RELEASE)-latest \
		.

.PHONY: blacken lint unittest test clean image-deps image-lint image-build
