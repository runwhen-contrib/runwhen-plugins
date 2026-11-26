# universal@1

The one runtime image for V13 skills. A skill names it as `universal@1`; the plugin registry
serves the newest `runtimes/universal/vX.Y.Z` release of major 1, with the image and its index
digest. Image: `ghcr.io/runwhen-contrib/runwhen-plugins-runtime-universal`.

## What is in it

Built on the rw-task base from `runwhen-contrib/rw-tasks-codecollection` (the pin in `BASE_IMAGE`,
see Dockerfile): python 3.12, the `runwhen-capability` SDK with `google-re2`, `kubectl`,
`yq`, `jq`, `curl`, `psql`, `redis-cli`, and the non-root `runwhen` user. This runtime adds:

| Added | How it is pinned |
| --- | --- |
| `git`, `tzdata` | Debian packages (not version-pinned; the pushed digest is the pin) |
| `gke-gcloud-auth-plugin` | Cloud SDK component tarball by revision, sha256 per arch |
| `helm`, `oc`, `flux`, `argocd` | release tarball or binary by version, sha256 per arch |
| `aws` (CLI v2) | `awscli-exe-linux-<arch>-<version>.zip`, sha256 per arch |
| `az` | PyPI into its own venv `/opt/az`, `requirements-az.txt` with hashes |
| `gcloud` | Google's apt repository at an exact version, repository key pinned by sha256; `bq`, `gsutil` and the bundled python are removed |
| `requests`, `PyYAML`, `jmespath`, `python-dateutil`, `kubernetes`, `boto3` | `requirements.txt` with hashes, installed into the SDK venv |

The Dockerfile has a comment with the vendor's checksum URL at every pinned download. Builds are
`linux/amd64` and `linux/arm64` only.

The image sets no `CLOUDSDK_CONFIG`, `AZURE_CONFIG_DIR` or AWS config path: the SDK points each CLI
at a private directory for the task run. Update checks, usage reporting, prompts, the aws pager
and az telemetry are off.

`requirements.in` repeats the versions the base already ships for what the SDK depends on, so the
lock never moves them. One exception: `python-dateutil` is `2.9.0.post0` (the base has 4.0.0)
because `botocore` requires `< 3`; `pip check` passes for the whole venv. `SDK_VERSION` in the
Dockerfile is a check, not an install: the build fails if the base ships another SDK version. To
move the SDK, move `BASE_IMAGE`, `SDK_VERSION` and `runtime.yaml` `tools.runwhen-capability` together.

## Size budget

Compressed 1.2 GiB (the registry layers), unpacked 3.0 GiB on amd64. The `smoke` job in
`runtime-universal.yml` fails above either: unpacked is `du` of the image's root filesystem,
compressed is `docker save | gzip`. The gcloud and az CLIs are most of it.

## Version and digest

`runtime.yaml` `version` is a semver and the git tag is `runtimes/universal/v<version>`. The
registry's identity for it is `(universal, major)`. Bumps follow `runtimes/README.md`: a new or
removed tool or a breaking change is a major, a new tool a minor, a tool patch or security rebuild
a patch. `RUNTIME_VERSION` in the Dockerfile (the `io.runwhen.runtime.version` label) equals it;
`ci/tests/test_universal_runtime.py` checks that. The digests in `runtime.yaml` are the pin that
reaches clusters. Until the first release PR they are the all-zero placeholder. The release flow is the one in
`runtimes/README.md` with `--runtime-dir runtimes/universal`.

Local check, native architecture:

```
docker build -t rwp-runtime-universal:dev runtimes/universal
docker run --rm -i --entrypoint sh rwp-runtime-universal:dev -s < runtimes/universal/smoke.sh
docker run --rm -i --user 65532 --read-only --tmpfs /work:rw,mode=1777 --entrypoint sh rwp-runtime-universal:dev -s < runtimes/universal/smoke.sh
python ci/check_runtime.py validate --allow-placeholder --runtime-dir runtimes/universal
```

## Custom images

A workspace can run its own image (an admin registers it in the plugin registry's
`pluginRegistry.config.runtimes` with the same fields as `runtime.yaml`, then names it
`<name>@<major>`). The contract:

1. Build `FROM ghcr.io/runwhen-contrib/runwhen-plugins-runtime-universal@sha256:<digest>`.
2. Keep the base's `CMD` (`rwtask serve ... --allow-bundles`), `ENTRYPOINT` and `/work`.
3. Run as a non-root user and work with uid 65532 and a read-only root filesystem (`/work` is the
   only writable mount).
4. Add tools only: remove or replace nothing the universal runtime ships.
5. Label the image `io.runwhen.runtime.base=universal@1`.
6. Run `smoke.sh` in your own CI, both plain and as `--user 65532 --read-only --tmpfs /work`:
   `docker run --rm -i --entrypoint sh <your image> -s < runtimes/universal/smoke.sh`. It is the
   conformance check.
7. Push to a registry your org trusts. The platform pulls only from the registries in the org policy
   (`orgPolicy.trustedRuntimeRegistries`) and checks that on every task run.

The platform does not read the `FROM` line or the label yet; the trusted registry is the control.
