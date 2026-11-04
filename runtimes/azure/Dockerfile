# azure@1 -- the runtime image for V13 plugin tasks that drive Azure (subscriptions, Key Vault, ...).
#
# Built on today's rw-task image (runwhen-contrib/rw-tasks-codecollection, branch
# v13/plugins): python 3.12, the runwhen-capability SDK at the pinned contracts (with
# google-re2), jq, curl and the non-root `runwhen` user (uid 1000). This image adds only the
# Azure CLI (`az`), pinned. kubectl, aws and gcloud are not installed here (the base ships
# kubectl; this runtime does not rely on it).
#
# FROM is the same pin as runtimes/kubernetes: the multi-arch INDEX digest of tag
# v13-plugins-m3-7c8bcb2, resolved with
#   docker buildx imagetools inspect ghcr.io/runwhen-contrib/rw-tasks-codecollection:v13-plugins-m3-7c8bcb2
# on 2026-11-05 (runwhen-capability b18be9c: contracts-v7). Move it together with the other
# runtimes, and bump runtime.yaml's version.
ARG BASE_IMAGE=ghcr.io/runwhen-contrib/rw-tasks-codecollection@sha256:f2e1de09241d731c9e49fe04c75d9da1cb839f7dc7197a9c627bf07f61851a09
FROM ${BASE_IMAGE}

# az is a Python application with a few hundred dependencies. It goes into its own virtualenv
# so none of them can replace what the runwhen-capability SDK in /opt/venv imports, and so the
# pin below is the whole of what changes. The Microsoft apt repository is not used: it publishes
# no package for Debian trixie that we could pin the same way. requirements.txt is the hash lock of
# azure-cli==${AZURE_CLI_VERSION} and all its dependencies (how it is generated is at its top):
# `--require-hashes --no-deps` installs exactly those files and nothing resolved at build time. The
# version is the pin; the pushed image digests in runtime.yaml are what reaches clusters. The base
# ends on a non-root user, so switch to root for the install and back after it.
ARG AZURE_CLI_VERSION=2.92.0
COPY requirements.txt /tmp/az-requirements.txt
USER root
RUN grep -q "^azure-cli==${AZURE_CLI_VERSION} " /tmp/az-requirements.txt \
    && python3 -m venv /opt/az \
    && /opt/az/bin/pip install --no-cache-dir --require-hashes --no-deps -r /tmp/az-requirements.txt \
    && /opt/az/bin/pip check \
    && rm /tmp/az-requirements.txt \
    && ln -s /opt/az/bin/az /usr/local/bin/az
USER runwhen

# No AZURE_CONFIG_DIR default: az's state is the session an auth script created in the task's private
# secrets directory (the `azure/cli` credential sets AZURE_CONFIG_DIR to it), and a task without that
# credential has no session, which is correct. A default under /work would leave a token cache where
# the next task could find it. Telemetry off; an extension is never installed on the fly (a task could
# not write it anyway, and a prompt would hang a run).
ENV AZURE_CORE_COLLECT_TELEMETRY=false \
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
