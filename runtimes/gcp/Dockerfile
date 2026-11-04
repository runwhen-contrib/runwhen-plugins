# gcp@1 -- the runtime image for V13 plugin tasks that drive Google Cloud with the gcloud CLI.
#
# Built on today's rw-task image (runwhen-contrib/rw-tasks-codecollection, branch
# v13/plugins): python 3.12, the runwhen-capability SDK at the pinned contracts (with
# google-re2), jq, curl and the non-root `runwhen` user (uid 1000). This image adds only the
# Google Cloud CLI (`gcloud`) at a pinned version, without the `gsutil` and `bq` tools and the
# bundled python that the package brings (see below). The base's own
# kubectl and psql stay (the image does not remove them, and no gcp task uses them). The other
# cloud CLIs, git, helm and gke-gcloud-auth-plugin are absent: the last belongs to the kubernetes
# runtime.
#
# FROM is the same base pin as runtimes/kubernetes, the multi-arch INDEX digest of tag
# v13-plugins-d1c7d04, resolved with
#   docker buildx imagetools inspect ghcr.io/runwhen-contrib/rw-tasks-codecollection:v13-plugins-d1c7d04
# on 2026-11-04 (runwhen-capability 6f6031d: contracts-v3). Move both Dockerfiles together
# (runtimes/README.md).
ARG BASE_IMAGE=ghcr.io/runwhen-contrib/rw-tasks-codecollection@sha256:f578706cb6f8d38d2776223618713c1f379c6c92a985ae2d7d2d6ca8122906af
FROM ${BASE_IMAGE}

# google-cloud-cli comes from Google's apt repository (amd64 and arm64). The version is pinned
# exactly: Google keeps old versions in the repository, unlike a Debian point release. The
# repository key is armored, which apt reads from a `.asc` file without gnupg; it is pinned by
# sha256, so a key that Google rotates (or a tampered download) fails the build instead of being
# trusted. To move it: download https://packages.cloud.google.com/apt/doc/apt-key.gpg, check the
# new key's fingerprint against Google's install page, and put its sha256 in GCLOUD_APT_REPO_SHA256.
# The base ends on a non-root user, so switch to root for the install and back after it. The base
# sets TMPDIR=/work/tmp, which does not exist at build time, and the package's post-install script
# needs a temporary directory: point it at /tmp.
#
# The package also installs gsutil and bq (82 MB and 22 MB) and its own python 3.14 (124 MB).
# No gcp task uses them (the plugin uses `gcloud storage`; gsutil keeps its own credential store
# and bq is a separate tool), and a tool that is not there cannot be reached by mistake, so they
# are removed in the SAME RUN as the install (a later RUN would only hide them in a lower layer).
# gcloud runs on the base's python3 (3.12, inside gcloud's supported range 3.9 to 3.14):
# CLOUDSDK_PYTHON below says so.
ARG GCLOUD_VERSION=592.0.0
ARG GCLOUD_APT_REPO_SHA256=3ecc63922b7795eb23fdc449ff9396f9114cb3cf186d6f5b53ad4cc3ebfbb11f
USER root
RUN install -d -m 0755 /usr/share/keyrings \
    && curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg -o /usr/share/keyrings/cloud.google.asc \
    && echo "${GCLOUD_APT_REPO_SHA256}  /usr/share/keyrings/cloud.google.asc" | sha256sum -c - \
    && echo "deb [signed-by=/usr/share/keyrings/cloud.google.asc] https://packages.cloud.google.com/apt cloud-sdk main" \
        > /etc/apt/sources.list.d/google-cloud-sdk.list \
    && apt-get update \
    && TMPDIR=/tmp apt-get install -y --no-install-recommends "google-cloud-cli=${GCLOUD_VERSION}-0" \
    && rm -rf /usr/lib/google-cloud-sdk/platform/gsutil /usr/lib/google-cloud-sdk/platform/bq \
        /usr/lib/google-cloud-sdk/platform/bundledpythonunix \
        /usr/lib/google-cloud-sdk/bin/gsutil /usr/lib/google-cloud-sdk/bin/bq /usr/bin/gsutil /usr/bin/bq \
    && rm -rf /var/lib/apt/lists/*
USER runwhen

# The runner starts the image with a read-only root and /work as the only writable mount, and
# gcloud writes its configuration, logs and credential cache: keep them in /work. A task's
# credential reaches gcloud as CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE (type gcp/adc), never
# through `gcloud auth login`. No update check, no usage reporting, no prompts: the CLI is pinned
# and must never wait for a person or phone home.
ENV CLOUDSDK_CONFIG=/work/.config/gcloud \
    CLOUDSDK_PYTHON=python3 \
    CLOUDSDK_COMPONENT_MANAGER_DISABLE_UPDATE_CHECK=true \
    CLOUDSDK_CORE_DISABLE_USAGE_REPORTING=true \
    CLOUDSDK_CORE_DISABLE_PROMPTS=1

# The base labels itself as the rw-task capability. A catalog scan reads those labels off the
# image, so blank the ones that would make this image look like rw-task (a label cannot be
# removed in a child image, only emptied) and say what it is.
LABEL io.runwhen.capability="" \
      io.runwhen.sbom="" \
      com.runwhen.capability.manifest.v1="" \
      com.runwhen.capability.schemas.v1="" \
      com.runwhen.rw-task.toolbox="" \
      io.runwhen.runtime="gcp"

ARG REVISION=unknown
ARG CREATED=unknown
LABEL org.opencontainers.image.title="runwhen-plugins-runtime-gcp" \
      org.opencontainers.image.description="gcp@1 runtime image for RunWhen V13 plugin tasks" \
      org.opencontainers.image.source="https://github.com/runwhen-contrib/runwhen-plugins" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${CREATED}"

# ENTRYPOINT, CMD, WORKDIR, TMPDIR and /work are inherited from the base: the runner starts the
# image like rw-task (`rwtask serve`, read-only root, /work as the only writable mount).
# The plugin registry reads runtime.yaml at tag runtimes/gcp/v<version> (see runtimes/README.md).
