# kubernetes@1 -- the runtime image for V13 plugin tasks that drive a Kubernetes cluster.
#
# Built on today's rw-task image (runwhen-contrib/rw-tasks-codecollection, branch
# v13/plugins): python 3.12, the runwhen-capability SDK at the pinned contracts (with
# google-re2), kubectl, jq, curl and the non-root `runwhen` user (uid 1000). This image adds what
# the kubernetes plugin needs on top of that: git (to read GitOps sources), tzdata (CronJob
# spec.timeZone is resolved with zoneinfo) and gke-gcloud-auth-plugin (the `gke` auth method).
# oc, helm, flux and argocd are deliberately absent.
#
# FROM is pinned by the multi-arch INDEX digest of tag v13-plugins-m3-7c8bcb2, resolved with
#   docker buildx imagetools inspect ghcr.io/runwhen-contrib/rw-tasks-codecollection:v13-plugins-m3-7c8bcb2
# on 2026-11-05 (runwhen-capability b18be9c: contracts-v7). Re-resolve and bump it (and
# runtime.yaml's version) when the SDK pin moves.
ARG BASE_IMAGE=ghcr.io/runwhen-contrib/rw-tasks-codecollection@sha256:f2e1de09241d731c9e49fe04c75d9da1cb839f7dc7197a9c627bf07f61851a09
FROM ${BASE_IMAGE}

# The base is Debian (trixie) with apt, so git and tzdata come from the distribution. They are
# not version-pinned here: a pinned Debian point release disappears from the mirror on the next
# security update and breaks the build. The pushed image digests in runtime.yaml are the pin.
# The base ends on a non-root user, so switch to root for the install and back after it.
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends git tzdata \
    && rm -rf /var/lib/apt/lists/*

# gke-gcloud-auth-plugin (the kubernetes plugin's `gke` auth method writes a kubeconfig that runs
# it, and mints a token with it): Google's own release tarball of the Cloud SDK component, pinned
# by revision and checked against the sha256 Google publishes for that revision in
#   https://dl.google.com/dl/cloudsdk/channels/rapid/components-2.json
# (component gke-gcloud-auth-plugin-linux-<arch>; version 0.5.21 in Cloud SDK 592.0.0). A revision
# stays downloadable, unlike a pinned apt version. It is a static Go binary, so no gcloud is
# needed: the kubeconfig runs it with --use_application_default_credentials. `x86_64` is the
# amd64 build and `arm` the arm64 one (an aarch64 binary). To move it, take the new revision and
# both checksums from components-2.json.
ARG GKE_PLUGIN_REVISION=20261030071842
ARG GKE_PLUGIN_SHA256_AMD64=b8d3bf3ebce3aff0146d85457146869ea66da7dc62d291e176cb88b6e73dd452
ARG GKE_PLUGIN_SHA256_ARM64=773ce0d886108cc47d19816449948b4187bc2b54bb26ec55729e2f34e3f45dcc
ARG TARGETARCH
RUN set -eu; \
    case "${TARGETARCH}" in \
      amd64) arch=x86_64; sha256="${GKE_PLUGIN_SHA256_AMD64}" ;; \
      arm64) arch=arm; sha256="${GKE_PLUGIN_SHA256_ARM64}" ;; \
      *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    tarball="google-cloud-sdk-gke-gcloud-auth-plugin-linux-${arch}-${GKE_PLUGIN_REVISION}.tar.gz"; \
    curl -fsSL --retry 3 -o "/tmp/${tarball}" \
      "https://dl.google.com/dl/cloudsdk/channels/rapid/components/${tarball}"; \
    echo "${sha256}  /tmp/${tarball}" | sha256sum -c -; \
    tar -xzf "/tmp/${tarball}" -C /usr/local bin/gke-gcloud-auth-plugin; \
    rm -f "/tmp/${tarball}"; \
    chmod 0755 /usr/local/bin/gke-gcloud-auth-plugin
USER runwhen

# The base labels itself as the rw-task capability. A catalog scan reads those labels off the
# image, so blank the ones that would make this image look like rw-task (a label cannot be
# removed in a child image, only emptied) and say what it is.
LABEL io.runwhen.capability="" \
      io.runwhen.sbom="" \
      com.runwhen.capability.manifest.v1="" \
      com.runwhen.capability.schemas.v1="" \
      com.runwhen.rw-task.toolbox="" \
      io.runwhen.runtime="kubernetes"

ARG REVISION=unknown
ARG CREATED=unknown
LABEL org.opencontainers.image.title="runwhen-plugins-runtime-kubernetes" \
      org.opencontainers.image.description="kubernetes@1 runtime image for RunWhen V13 plugin tasks" \
      org.opencontainers.image.source="https://github.com/runwhen-contrib/runwhen-plugins" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${CREATED}"

# ENTRYPOINT, CMD, WORKDIR, TMPDIR and /work are inherited from the base: the runner starts the
# image like rw-task (`rwtask serve`, read-only root, /work as the only writable mount).
