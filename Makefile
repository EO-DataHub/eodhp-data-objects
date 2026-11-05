VERSION ?= latest
IMAGENAME = eodhp-data-objects
DOCKERREPO ?= public.ecr.aws/eodh
uv-run ?= uv run --no-sync

.PHONY: check
check:
	${uv-run} ruff check
	${uv-run} ruff format --check
	${uv-run} pyright
	${uv-run} pytest

.PHONY: test
test:
	${uv-run} pytest

.PHONY: dockerbuild
dockerbuild:
	DOCKER_BUILDKIT=1 docker build -t ${IMAGENAME}:${VERSION} .

.PHONY: dockerpush
dockerpush: dockerbuild
	docker tag ${IMAGENAME}:${VERSION} ${DOCKERREPO}/${IMAGENAME}:${VERSION}
	docker push ${DOCKERREPO}/${IMAGENAME}:${VERSION}
