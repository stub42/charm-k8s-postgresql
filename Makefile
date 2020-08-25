DOCKER_IMAGE ?= localhost:32000/pgcharm
DOCKER_TAG ?= pg$(PG_VER)-latest

blacken:
	@echo "Normalising python layout with black."
	@tox -e black


lint: blacken
	@echo "Running flake8"
	@tox -e lint

# We actually use the build directory created by charmcraft,
# but the .charm file makes a much more convenient sentinel.
unittest: bind.charm
	tox -e unit

test: lint unittest

clean:
	@echo "Cleaning files"
	git clean -fXd

postgresql.charm: src/*.py requirements.txt *.yaml
	charmcraft build

image-deps:
	@echo "Checking shellcheck is present."
	@command -v shellcheck >/dev/null || { echo "Please install shellcheck to continue ('sudo snap install shellcheck')" && false; }

image-lint: image-deps
	@echo "Running shellcheck."
	shellcheck files/docker-entrypoint.sh
	shellcheck files/docker-readyness.sh

image-build: image-lint
	@echo "Building the $(DOCKER_IMAGE):$(DOCKER_TAG) image"
	docker build \
		--build-arg BUILD_DATE=$$(date -u +'%Y-%m-%dT%H:%M:%SZ') \
		-t $(DOCKER_IMAGE):$(DOCKER_TAG) \
		.

image-push-registry:
	@echo "Pushing the image to registry."
	docker push $(DOCKER_IMAGE):$(DOCKER_TAG)

image-push-microk8s: image-build
	@echo "Pushing the image to microk8s local storage."
	docker save $(DOCKER_IMAGE):$(DOCKER_TAG) > .pgimg.tar && microk8s.ctr image import .pgimg.tar && rm -v .pgimg.tar

.PHONY: blacken lint unittest test clean image-deps image-lint image-build image-push
