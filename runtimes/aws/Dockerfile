# aws@1 -- the runtime image for V13 plugin tasks that call AWS through the aws CLI v2.
#
# Built on today's rw-task image (runwhen-contrib/rw-tasks-codecollection, branch
# v13/plugins): python 3.12, the runwhen-capability SDK at the pinned contracts (with
# google-re2), jq, curl and the non-root `runwhen` user (uid 1000). This image adds only the
# AWS CLI v2. kubectl is inherited from the base and is not removed; no aws plugin task uses it
# (an EKS kubeconfig comes from the kubernetes plugin's eks method, which runs in the
# kubernetes@1 runtime). boto3, the AWS SDKs, sam, cdk and eksctl are deliberately absent.
#
# FROM is pinned by the multi-arch INDEX digest of tag v13-plugins-m3-7c8bcb2, the same pin as
# runtimes/kubernetes (see there for how to re-resolve it). Re-resolve and bump it (and
# runtime.yaml's version) when the SDK pin moves.
ARG BASE_IMAGE=ghcr.io/runwhen-contrib/rw-tasks-codecollection@sha256:f2e1de09241d731c9e49fe04c75d9da1cb839f7dc7197a9c627bf07f61851a09
FROM ${BASE_IMAGE}

# AWS CLI v2 is not in Debian. It ships as a self-contained bundle (its own python), downloaded
# from awscli.amazonaws.com at a fixed version and verified against the sha256 recorded here
# (computed from the downloaded zips on 2026-11-04). To move the CLI, change the version and both
# checksums together: `curl -sO https://awscli.amazonaws.com/awscli-exe-linux-<x86_64|aarch64>-<version>.zip`
# then `shasum -a 256` each. unzip is installed for the install only and removed again.
ARG AWSCLI_VERSION=2.39.3
ARG AWSCLI_SHA256_AMD64=14cb35809ad7547dd3c69fa9899a3975fbd850b6a6791f3fe264856490cd9a63
ARG AWSCLI_SHA256_ARM64=f6eb845fbacb11bee87de3f5fbe8043773cbe89d0414607b44b71e216eb9226b
ARG TARGETARCH

# The base ends on a non-root user, so switch to root for the install and back after it.
USER root
RUN set -eu; \
    case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
      amd64) arch=x86_64; sha="${AWSCLI_SHA256_AMD64}" ;; \
      arm64) arch=aarch64; sha="${AWSCLI_SHA256_ARM64}" ;; \
      *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends unzip; \
    cd /tmp; \
    curl -fsSL -o awscliv2.zip "https://awscli.amazonaws.com/awscli-exe-linux-${arch}-${AWSCLI_VERSION}.zip"; \
    echo "${sha}  awscliv2.zip" | sha256sum -c -; \
    unzip -q awscliv2.zip; \
    ./aws/install --install-dir /usr/local/aws-cli --bin-dir /usr/local/bin; \
    rm -rf /usr/local/aws-cli/v2/current/dist/awscli/examples /tmp/aws /tmp/awscliv2.zip; \
    apt-get purge -y --auto-remove unzip; \
    rm -rf /var/lib/apt/lists/*
USER runwhen

# Never page output: the CLI is run without a TTY by tasks, and a pager would hang a run.
ENV AWS_PAGER=""

# The base labels itself as the rw-task capability. A catalog scan reads those labels off the
# image, so blank the ones that would make this image look like rw-task (a label cannot be
# removed in a child image, only emptied) and say what it is.
LABEL io.runwhen.capability="" \
      io.runwhen.sbom="" \
      com.runwhen.capability.manifest.v1="" \
      com.runwhen.capability.schemas.v1="" \
      com.runwhen.rw-task.toolbox="" \
      io.runwhen.runtime="aws"

ARG REVISION=unknown
ARG CREATED=unknown
LABEL org.opencontainers.image.title="runwhen-plugins-runtime-aws" \
      org.opencontainers.image.description="aws@1 runtime image for RunWhen V13 plugin tasks" \
      org.opencontainers.image.source="https://github.com/runwhen-contrib/runwhen-plugins" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${CREATED}"

# ENTRYPOINT, CMD, WORKDIR, TMPDIR and /work are inherited from the base: the runner starts the
# image like rw-task (`rwtask serve`, read-only root, /work as the only writable mount).
