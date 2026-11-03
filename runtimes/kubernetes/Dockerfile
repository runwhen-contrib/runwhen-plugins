# kubernetes@1 -- the runtime image for V13 plugin tasks that drive a Kubernetes cluster.
#
# Built on today's rw-task image (runwhen-contrib/rw-tasks-codecollection, branch
# v13/plugins): python 3.12, the runwhen-capability SDK at the pinned contracts (with
# google-re2), kubectl, jq, curl and the non-root `runwhen` user (uid 1000). This image adds what
# the kubernetes plugin needs on top of that: git (to read GitOps sources) and tzdata (CronJob
# spec.timeZone is resolved with zoneinfo). oc, helm, flux and argocd are deliberately absent.
#
# FROM is pinned by the multi-arch INDEX digest of tag v13-plugins-7b2a37a, resolved with
#   docker buildx imagetools inspect ghcr.io/runwhen-contrib/rw-tasks-codecollection:v13-plugins-7b2a37a
# on 2026-11-04. Re-resolve and bump it (and runtime.yaml's version) when the SDK pin moves.
ARG BASE_IMAGE=ghcr.io/runwhen-contrib/rw-tasks-codecollection@sha256:0178e6914a637dbbc720efa7e9b09d00ea02b84e24dd3987b4a4a2a4a799e233
FROM ${BASE_IMAGE}

# The base is Debian (trixie) with apt, so git and tzdata come from the distribution. They are
# not version-pinned here: a pinned Debian point release disappears from the mirror on the next
# security update and breaks the build. The pushed image digests in runtime.yaml are the pin.
# The base ends on a non-root user, so switch to root for the install and back after it.
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends git tzdata \
    && rm -rf /var/lib/apt/lists/*
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
