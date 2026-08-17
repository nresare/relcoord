FROM cleanstart/helm:4.2.3 AS helm

FROM astral/uv:trixie-slim AS builder

WORKDIR /build

COPY pyproject.toml uv.lock README.md ./
# Interestingly, uv build and uv install will look at VIRTUAL_ENV but not uv venv
ENV VIRTUAL_ENV=/deps-venv
ENV UV_PYTHON_INSTALL_DIR=/python
# Install dependencies in a separate layer (cached when lock file unchanged)
RUN uv venv /deps-venv && uv sync --frozen --no-install-project --no-dev --active

COPY src ./src/

# The version is passed in because pyproject.toml takes it from the nearest
# reachable git tag, and .git is deliberately not part of the build context: it
# would have to be copied in to be read, which would rebuild the layers above on
# every commit, and the tag naming this image is a hash of the context that git
# is left out of. Declared here rather than at the top of the stage so that a new
# version rebuilds the wheel without invalidating the dependency layer.
ARG RELCOORD_VERSION=0.0.0
ENV SETUPTOOLS_SCM_PRETEND_VERSION=$RELCOORD_VERSION
RUN uv build --wheel
ENV VIRTUAL_ENV=/venv
RUN uv venv /venv && uv pip install --no-deps dist/*.whl

# The tag this image is published under, which only the build knows: it names the
# release and the build both, where the version above names the release alone.
# The final image has no shell to write it with, so it is written here, last, and
# copied in below. An unset tag leaves the file empty, which relcoord reports as
# no tag rather than as a tag of nothing.
ARG RELCOORD_IMAGE_TAG=""
RUN mkdir -p /image && printf '%s' "$RELCOORD_IMAGE_TAG" > /image/image-tag

FROM gcr.io/distroless/cc-debian13

COPY --from=builder /python /python
# This might look a little magical, but it will ensure that the files from deps-venv (created above)
# ends up in a separate layer, only updating it when needed.
COPY --from=builder /deps-venv /venv
COPY --from=builder /venv /venv
COPY --from=builder /image/image-tag /usr/share/relcoord/image-tag
COPY --from=helm /usr/bin/helm /usr/local/bin/helm

EXPOSE 8080
USER nonroot
ENTRYPOINT ["/venv/bin/relcoord"]

