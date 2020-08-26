# Copyright 2020 Canonical Ltd.
# Licensed under the GPLv3, see LICENCE file for details.

PG_MAJOR := 12
DIST_RELEASE := focal

IMAGE_REGISTRY :=
IMAGE_NAME := pgcharm
IMAGE_TAG := pg$(PG_MAJOR)-latest

REGISTRY_IMAGE := $(IMAGE_REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)
LOCAL_IMAGE := $(IMAGE_NAME):$(IMAGE_TAG)

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
	@echo "Building the $(LOCAL_IMAGE) image"
	docker build -t $(LOCAL_IMAGE) --build-arg BUILD_DATE=$$(date -u +'%Y-%m-%dT%H:%M:%SZ') .

image-push-microk8s: image-build
	@echo "Pushing the $(LOCAL_IMAGE) image to microk8s local storage."
	docker save $(LOCAL_IMAGE) > .pgimg.tar && microk8s.ctr image import .pgimg.tar && rm -v .pgimg.tar

image-push-registry: image-build
	@echo "Pushing the $(LOCAL_IMAGE) image to $(IMAGE_REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)."
	docker tag $(LOCAL_IMAGE) $(REGISTRY_IMAGE)
	docker push $(REGISTRY_IMAGE)

.PHONY: blacken lint unittest test clean image-deps image-lint image-build image-push
