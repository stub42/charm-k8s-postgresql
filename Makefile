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

PG_MAJOR := 12
DIST_RELEASE := focal

IMAGE_REGISTRY :=
IMAGE_NAME := pgcharm
IMAGE_TAG := latest
NO_CACHE :=
# NO_CACHE := --no-cache

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
unittest: postgresql.charm
	tox -e unit

test: lint unittest

clean:
	@echo "Cleaning files"
	git clean -fXd

postgresql.charm: src/*.py requirements.txt *.yaml .jujuignore
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
	docker build $(NO_CACHE) -t $(LOCAL_IMAGE) --build-arg BUILD_DATE=$$(date -u +'%Y-%m-%dT%H:%M:%SZ') .

image-push-microk8s: image-build
	@echo "Pushing the $(LOCAL_IMAGE) image to microk8s local storage."
	docker save $(LOCAL_IMAGE) > .pgimg.tar && microk8s.ctr image import .pgimg.tar && rm -v .pgimg.tar

image-push-registry: image-build
	@echo "Pushing the $(LOCAL_IMAGE) image to $(IMAGE_REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)."
	docker tag $(LOCAL_IMAGE) $(REGISTRY_IMAGE)
	docker push $(REGISTRY_IMAGE)

.PHONY: blacken lint unittest test clean image-deps image-lint image-build image-push
