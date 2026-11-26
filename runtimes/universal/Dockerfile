# universal@1 -- the one runtime image for V13 skills: Kubernetes, GitOps and the three cloud CLIs,
# plus the python libraries skills commonly import.
#
# Built on the rw-task image (runwhen-contrib/rw-tasks-codecollection, branch v13/plugins): python
# 3.12, the runwhen-capability SDK (with google-re2), kubectl, yq, jq, curl, psql, redis-cli and the
# non-root `runwhen` user (uid 1000). This image adds:
#   git, tzdata, gke-gcloud-auth-plugin, helm, oc, flux, argocd, aws CLI v2, az (own venv),
#   gcloud (without bq, gsutil and its bundled python), and hash-pinned python libraries.
# Every downloaded binary is pinned by version and by sha256 per architecture (amd64, arm64).
#
# FROM is the multi-arch INDEX digest of tag v13-plugins-m4-adfa879, the same pin as
# runtimes/kubernetes (see runtimes/README.md for how to re-resolve it).
ARG BASE_IMAGE=ghcr.io/runwhen-contrib/rw-tasks-codecollection@sha256:9f3273027b51da860a18e75252d15fd398010df2c5fb67de3a32f4861ed7cc19
FROM ${BASE_IMAGE}

# The SDK comes with the base, so this ARG is a check, not an install: the build fails if the base
# ships another runwhen-capability version than the one this runtime says it carries (runtime.yaml
# tools.runwhen-capability). Moving the SDK means moving BASE_IMAGE and this value together.
ARG SDK_VERSION=0.4.0
ARG TARGETARCH

# The base is Debian (trixie) with apt, so git and tzdata come from the distribution. They are not
# version-pinned: a pinned Debian point release disappears from the mirror on the next security
# update and breaks the build. The pushed image digests in runtime.yaml are the pin. The base ends
# on a non-root user, so every install below switches to root and the last one switches back.
USER root
RUN set -eu; \
    installed="$(python3 -c 'import importlib.metadata as m; print(m.version("runwhen-capability"))')"; \
    [ "${installed}" = "${SDK_VERSION}" ] || { echo "base ships runwhen-capability ${installed}, SDK_VERSION is ${SDK_VERSION}" >&2; exit 1; }; \
    apt-get update; \
    apt-get install -y --no-install-recommends git tzdata; \
    rm -rf /var/lib/apt/lists/*

# gke-gcloud-auth-plugin (the kubernetes plugin's `gke` auth method writes a kubeconfig that runs
# it): Google's release tarball of the Cloud SDK component, pinned by revision and checked against
# the sha256 Google publishes in
#   https://dl.google.com/dl/cloudsdk/channels/rapid/components-2.json
# (component gke-gcloud-auth-plugin-linux-<arch>; version 0.5.21 in Cloud SDK 592.0.0). A static Go
# binary, so no gcloud is needed. `x86_64` is the amd64 build and `arm` the arm64 one.
ARG GKE_PLUGIN_REVISION=20261030071842
ARG GKE_PLUGIN_SHA256_AMD64=b8d3bf3ebce3aff0146d85457146869ea66da7dc62d291e176cb88b6e73dd452
ARG GKE_PLUGIN_SHA256_ARM64=773ce0d886108cc47d19816449948b4187bc2b54bb26ec55729e2f34e3f45dcc
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

# helm. Checksums: https://get.helm.sh/helm-v<HELM_VERSION>-linux-<arch>.tar.gz.sha256sum
ARG HELM_VERSION=4.3.2
ARG HELM_SHA256_AMD64=d1c0278d46beab79582d7e50f5b3dc206944ba8e577cde618513fd07daa53983
ARG HELM_SHA256_ARM64=df31cb61680ef728f82c770e5280dd565617bee6b039d37e85c677288be4bbb0
RUN set -eu; \
    case "${TARGETARCH}" in \
      amd64) sha256="${HELM_SHA256_AMD64}" ;; \
      arm64) sha256="${HELM_SHA256_ARM64}" ;; \
      *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL --retry 3 -o /tmp/helm.tgz "https://get.helm.sh/helm-v${HELM_VERSION}-linux-${TARGETARCH}.tar.gz"; \
    echo "${sha256}  /tmp/helm.tgz" | sha256sum -c -; \
    tar -xzf /tmp/helm.tgz -C /usr/local/bin --strip-components=1 "linux-${TARGETARCH}/helm"; \
    rm -f /tmp/helm.tgz; \
    chmod 0755 /usr/local/bin/helm

# oc, the OpenShift client (the tarball also holds a kubectl, which is left out: the base's stays).
# Checksums: https://mirror.openshift.com/pub/openshift-v4/clients/ocp/<OC_VERSION>/sha256sum.txt
# (the rhel9 builds: they need glibc 2.34 or newer, Debian trixie has 2.41).
ARG OC_VERSION=5.0.1
ARG OC_SHA256_AMD64=13df492a60126d3274d8f48743d441e927b0e3b28de63cb5ba09e7db052cd356
ARG OC_SHA256_ARM64=77a0e93ef62716fcf057a5d336a7f715c066013df792241489df7b0da18a92f4
RUN set -eu; \
    case "${TARGETARCH}" in \
      amd64) sha256="${OC_SHA256_AMD64}" ;; \
      arm64) sha256="${OC_SHA256_ARM64}" ;; \
      *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    tarball="openshift-client-linux-${TARGETARCH}-rhel9-${OC_VERSION}.tar.gz"; \
    curl -fsSL --retry 3 -o "/tmp/${tarball}" \
      "https://mirror.openshift.com/pub/openshift-v4/clients/ocp/${OC_VERSION}/${tarball}"; \
    echo "${sha256}  /tmp/${tarball}" | sha256sum -c -; \
    tar -xzf "/tmp/${tarball}" -C /usr/local/bin oc; \
    rm -f "/tmp/${tarball}"; \
    chmod 0755 /usr/local/bin/oc

# flux. Checksums: https://github.com/fluxcd/flux2/releases/download/v<FLUX_VERSION>/flux_<FLUX_VERSION>_checksums.txt
ARG FLUX_VERSION=2.10.1
ARG FLUX_SHA256_AMD64=967365ff84aabc97871827ff33713ef0c09f62dd83c18759e3a5e39adcea9285
ARG FLUX_SHA256_ARM64=da18ddb25c9614405d9e2f13ad014fd660bfcbe7218674cabf9325db73f811e2
RUN set -eu; \
    case "${TARGETARCH}" in \
      amd64) sha256="${FLUX_SHA256_AMD64}" ;; \
      arm64) sha256="${FLUX_SHA256_ARM64}" ;; \
      *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    tarball="flux_${FLUX_VERSION}_linux_${TARGETARCH}.tar.gz"; \
    curl -fsSL --retry 3 -o "/tmp/${tarball}" \
      "https://github.com/fluxcd/flux2/releases/download/v${FLUX_VERSION}/${tarball}"; \
    echo "${sha256}  /tmp/${tarball}" | sha256sum -c -; \
    tar -xzf "/tmp/${tarball}" -C /usr/local/bin flux; \
    rm -f "/tmp/${tarball}"; \
    chmod 0755 /usr/local/bin/flux

# argocd. Checksums: https://github.com/argoproj/argo-cd/releases/download/v<ARGOCD_VERSION>/cli_checksums.txt
ARG ARGOCD_VERSION=3.6.3
ARG ARGOCD_SHA256_AMD64=3750cd7ed2a2ef05c640344b86f29aa99c9783e847c8f326c81e3900ad39412b
ARG ARGOCD_SHA256_ARM64=04a7b681d6c5eec193159705c5556114f8cba98abada4c17c3991124065e1964
RUN set -eu; \
    case "${TARGETARCH}" in \
      amd64) sha256="${ARGOCD_SHA256_AMD64}" ;; \
      arm64) sha256="${ARGOCD_SHA256_ARM64}" ;; \
      *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL --retry 3 -o /usr/local/bin/argocd \
      "https://github.com/argoproj/argo-cd/releases/download/v${ARGOCD_VERSION}/argocd-linux-${TARGETARCH}"; \
    echo "${sha256}  /usr/local/bin/argocd" | sha256sum -c -; \
    chmod 0755 /usr/local/bin/argocd

# AWS CLI v2: a self-contained bundle from awscli.amazonaws.com at a fixed version, verified against
# the sha256 recorded here (computed from the downloaded zips on 2026-11-04). To move it, change the
# version and both checksums together: `curl -sO https://awscli.amazonaws.com/awscli-exe-linux-<x86_64|aarch64>-<version>.zip`
# then `shasum -a 256` each. unzip is installed for the install only and removed again.
ARG AWSCLI_VERSION=2.39.3
ARG AWSCLI_SHA256_AMD64=14cb35809ad7547dd3c69fa9899a3975fbd850b6a6791f3fe264856490cd9a63
ARG AWSCLI_SHA256_ARM64=f6eb845fbacb11bee87de3f5fbe8043773cbe89d0414607b44b71e216eb9226b
RUN set -eu; \
    case "${TARGETARCH}" in \
      amd64) arch=x86_64; sha="${AWSCLI_SHA256_AMD64}" ;; \
      arm64) arch=aarch64; sha="${AWSCLI_SHA256_ARM64}" ;; \
      *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends unzip; \
    cd /tmp; \
    curl -fsSL --retry 3 -o awscliv2.zip "https://awscli.amazonaws.com/awscli-exe-linux-${arch}-${AWSCLI_VERSION}.zip"; \
    echo "${sha}  awscliv2.zip" | sha256sum -c -; \
    unzip -q awscliv2.zip; \
    ./aws/install --install-dir /usr/local/aws-cli --bin-dir /usr/local/bin; \
    rm -rf /usr/local/aws-cli/v2/current/dist/awscli/examples /tmp/aws /tmp/awscliv2.zip; \
    apt-get purge -y --auto-remove unzip; \
    rm -rf /var/lib/apt/lists/*

# az is a Python application with a few hundred dependencies. It goes into its own virtualenv so none
# of them can replace what the SDK in /opt/venv imports. requirements-az.txt is the hash lock of
# azure-cli==${AZURE_CLI_VERSION} and all its dependencies (how it is generated is at its top):
# `--require-hashes --no-deps` installs exactly those files and nothing resolved at build time.
ARG AZURE_CLI_VERSION=2.92.0
COPY requirements-az.txt /tmp/az-requirements.txt
RUN grep -q "^azure-cli==${AZURE_CLI_VERSION} " /tmp/az-requirements.txt \
    && python3 -m venv /opt/az \
    && /opt/az/bin/pip install --no-cache-dir --require-hashes --no-deps -r /tmp/az-requirements.txt \
    && /opt/az/bin/pip check \
    && rm /tmp/az-requirements.txt \
    && ln -s /opt/az/bin/az /usr/local/bin/az

# gcloud: google-cloud-cli from Google's apt repository (amd64 and arm64) at an exact version, which
# Google keeps in the repository. The repository key is armored, which apt reads from a `.asc` file
# without gnupg; it is pinned by sha256, so a key that Google rotates (or a tampered download) fails
# the build instead of being trusted. To move it: download
# https://packages.cloud.google.com/apt/doc/apt-key.gpg, check the new key's fingerprint against
# Google's install page, and put its sha256 in GCLOUD_APT_REPO_SHA256. The base sets
# TMPDIR=/work/tmp, which does not exist at build time, and the package's post-install script needs
# a temporary directory: point it at /tmp.
# The package also installs gsutil and bq and its own python 3.14. A skill uses `gcloud storage`, and
# a tool that is not there cannot be reached by mistake, so all three are removed in the SAME RUN as
# the install (a later RUN would only hide them in a lower layer). gcloud runs on the base's python3
# (3.12, inside gcloud's supported range): CLOUDSDK_PYTHON below says so.
ARG GCLOUD_VERSION=592.0.0
ARG GCLOUD_APT_REPO_SHA256=3ecc63922b7795eb23fdc449ff9396f9114cb3cf186d6f5b53ad4cc3ebfbb11f
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

# Python libraries for skills, hash-locked into the SDK venv (/opt/venv): requests, PyYAML, jmespath,
# python-dateutil, kubernetes, boto3 and their dependencies. requirements.in says which versions
# follow the base and why (python-dateutil goes below 3 because botocore requires it). The lock
# repeats the base's own pins for what the SDK depends on, so `pip check` passes for both.
COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install --no-cache-dir --require-hashes --no-deps -r /tmp/requirements.txt \
    && /opt/venv/bin/pip check \
    && rm /tmp/requirements.txt
USER runwhen

# No CLOUDSDK_CONFIG, AZURE_CONFIG_DIR or AWS_* config paths: the SDK points each CLI at a private
# directory for the task run, and a default would leave state where the next run could find it.
# No update check, no usage reporting, no prompts, no pager, no telemetry: the CLIs are pinned and
# must never wait for a person or phone home; an az extension is never installed on the fly.
ENV CLOUDSDK_PYTHON=python3 \
    CLOUDSDK_COMPONENT_MANAGER_DISABLE_UPDATE_CHECK=true \
    CLOUDSDK_CORE_DISABLE_USAGE_REPORTING=true \
    CLOUDSDK_CORE_DISABLE_PROMPTS=1 \
    AWS_PAGER="" \
    AZURE_CORE_COLLECT_TELEMETRY=false \
    AZURE_EXTENSION_USE_DYNAMIC_INSTALL=no \
    AZURE_CORE_NO_COLOR=true

# The base labels itself as the rw-task skill host. A catalog scan reads those labels off the image,
# so blank the ones that would make this image look like rw-task (a label cannot be removed in a
# child image, only emptied) and say what it is. RUNTIME_VERSION equals runtime.yaml's version
# (ci/tests/test_universal_runtime.py checks it).
ARG RUNTIME_VERSION=1.0.0
LABEL io.runwhen.capability="" \
      io.runwhen.sbom="" \
      com.runwhen.capability.manifest.v1="" \
      com.runwhen.capability.schemas.v1="" \
      com.runwhen.rw-task.toolbox="" \
      io.runwhen.runtime="universal" \
      io.runwhen.runtime.version="${RUNTIME_VERSION}"

ARG REVISION=unknown
ARG CREATED=unknown
LABEL org.opencontainers.image.title="runwhen-plugins-runtime-universal" \
      org.opencontainers.image.description="universal@1 runtime image for RunWhen V13 plugin tasks" \
      org.opencontainers.image.source="https://github.com/runwhen-contrib/runwhen-plugins" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${CREATED}"

# ENTRYPOINT, CMD, WORKDIR, TMPDIR and /work are inherited from the base: the runner starts the
# image like rw-task (`rwtask serve`, read-only root, /work as the only writable mount).
# The plugin registry reads runtime.yaml at tag runtimes/universal/v<version> (see runtimes/README.md).
