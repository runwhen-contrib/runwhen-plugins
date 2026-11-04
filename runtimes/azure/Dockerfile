# azure@1 -- the runtime image for V13 plugin tasks that drive Azure (subscriptions, Key Vault, ...).
#
# Built on today's rw-task image (runwhen-contrib/rw-tasks-codecollection, branch
# v13/plugins): python 3.12, the runwhen-capability SDK at the pinned contracts (with
# google-re2), jq, curl and the non-root `runwhen` user (uid 1000). This image adds only the
# Azure CLI (`az`), pinned. kubectl, aws and gcloud are not installed here (the base ships
# kubectl; this runtime does not rely on it).
#
# FROM is the same pin as runtimes/kubernetes: the multi-arch INDEX digest of tag
# v13-plugins-d1c7d04, resolved with
#   docker buildx imagetools inspect ghcr.io/runwhen-contrib/rw-tasks-codecollection:v13-plugins-d1c7d04
# on 2026-11-04 (runwhen-capability 6f6031d: contracts-v3). Move it together with the other
# runtimes, and bump runtime.yaml's version.
ARG BASE_IMAGE=ghcr.io/runwhen-contrib/rw-tasks-codecollection@sha256:f578706cb6f8d38d2776223618713c1f379c6c92a985ae2d7d2d6ca8122906af
FROM ${BASE_IMAGE}

# az is a Python application with a few hundred dependencies. It goes into its own virtualenv
# so none of them can replace what the runwhen-capability SDK in /opt/venv imports, and so the
# pin below is the whole of what changes. The Microsoft apt repository is not used: it publishes
# no package for Debian trixie that we could pin the same way. The version is the pin; the pushed
# image digests in runtime.yaml are what reaches clusters. The base ends on a non-root user, so
# switch to root for the install and back after it.
ARG AZURE_CLI_VERSION=2.92.0
USER root
RUN python3 -m venv /opt/az \
    && /opt/az/bin/pip install --no-cache-dir "azure-cli==${AZURE_CLI_VERSION}" \
    && ln -s /opt/az/bin/az /usr/local/bin/az
USER runwhen

# az writes its token cache, logs and config under AZURE_CONFIG_DIR. The auth scripts point it at
# a directory under $RW_CREDENTIALS_DIR (memory-backed, deleted when the task ends); this default
# keeps a stray `az` from writing under the read-only root. Telemetry off; an extension is never
# installed on the fly (a task could not write it anyway, and a prompt would hang a run).
ENV AZURE_CONFIG_DIR=/work/.azure \
    AZURE_CORE_COLLECT_TELEMETRY=false \
    AZURE_EXTENSION_USE_DYNAMIC_INSTALL=no \
    AZURE_CORE_NO_COLOR=true

# The base labels itself as the rw-task capability. A catalog scan reads those labels off the
# image, so blank the ones that would make this image look like rw-task (a label cannot be
# removed in a child image, only emptied) and say what it is.
LABEL io.runwhen.capability="" \
      io.runwhen.sbom="" \
      com.runwhen.capability.manifest.v1="" \
      com.runwhen.capability.schemas.v1="" \
      com.runwhen.rw-task.toolbox="" \
      io.runwhen.runtime="azure"

ARG REVISION=unknown
ARG CREATED=unknown
LABEL org.opencontainers.image.title="runwhen-plugins-runtime-azure" \
      org.opencontainers.image.description="azure@1 runtime image for RunWhen V13 plugin tasks" \
      org.opencontainers.image.source="https://github.com/runwhen-contrib/runwhen-plugins" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${CREATED}"

# ENTRYPOINT, CMD, WORKDIR, TMPDIR and /work are inherited from the base: the runner starts the
# image like rw-task (`rwtask serve`, read-only root, /work as the only writable mount).
