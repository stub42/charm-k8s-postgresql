PG_VER = 12
DOCKER_IMAGE ?= localhost:32000/pgcharm
DOCKER_TAG ?= pg$(PG_VER)-latest

DOCKER_DEPS := postgresql repmgr postgresql-$(PG_VER)-repack repmgr openssh-server unattended-upgrades

ifeq ($(PG_VER),12)
    DIST_RELEASE = focal
else
    DIST_RELEASE ?= focal
endif

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
	@shellcheck files/docker-readyness.sh

image-build: image-lint
	@echo "Building the image."
	@docker build \
		--no-cache=true \
		--build-arg BUILD_DATE=$$(date -u +'%Y-%m-%dT%H:%M:%SZ') \
		--build-arg PKGS_TO_INSTALL='$(DOCKER_DEPS)' \
		--build-arg DIST_RELEASE=$(DIST_RELEASE) \
		--build-arg PG_VER=$(PG_VER) \
		-t $(DOCKER_IMAGE):$(DOCKER_TAG) \
		.

image-push: image-build
	@echo "Pushing the image."
	@docker push $(DOCKER_IMAGE):$(DOCKER_TAG)

.PHONY: blacken lint unittest test clean image-deps image-lint image-build image-push
