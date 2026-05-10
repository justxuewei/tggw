IMAGE ?= xavierniu/tggw
TAG ?= latest

.PHONY: build push test

build:
	docker build -t $(IMAGE):$(TAG) .

push: build
	docker push $(IMAGE):$(TAG)

test:
	uv run python -m unittest
