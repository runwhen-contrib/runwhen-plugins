# Runtimes

A runtime is the container image a V13 plugin task runs in. Each directory here holds one
runtime: a `Dockerfile` and a `runtime.yaml` that the plugin registry reads (tag
`runtimes/<name>/v<semver>`, file `runtimes/<name>/runtime.yaml`). A task names its runtime as
`<name>@<major>`; the registry serves the newest release of that major.

| Runtime | Image | For |
| --- | --- | --- |
| `kubernetes` | `ghcr.io/runwhen-contrib/runwhen-plugins-runtime-kubernetes` | `kubernetes` plugin tasks |
| `aws` | `ghcr.io/runwhen-contrib/runwhen-plugins-runtime-aws` | `aws` plugin tasks |
| `azure` | `ghcr.io/runwhen-contrib/runwhen-plugins-runtime-azure` | `azure` plugin tasks |

## kubernetes

Built on the rw-task image from `runwhen-contrib/rw-tasks-codecollection` (branch `v13/plugins`):
python 3.12, the `runwhen-capability` SDK with `google-re2`, `kubectl`, `jq`, `curl`, and the
non-root `runwhen` user (uid 1000; the runner runs it as uid 65532 with a read-only root and
`/work` writable). The Dockerfile adds `git` and `tzdata` (CronJob `spec.timeZone` is resolved
with `zoneinfo`), and `gke-gcloud-auth-plugin` (the kubernetes plugin's `gke` auth method writes a
kubeconfig that runs it) from Google's pinned Cloud SDK component tarball, checked against the sha256
Google publishes. `oc`, `helm`, `flux` and `argocd` are left out on purpose.

`FROM` is pinned by the base's multi-arch index digest (`v13-plugins-d1c7d04`). To move the base,
re-resolve it:

```
docker buildx imagetools inspect ghcr.io/runwhen-contrib/rw-tasks-codecollection:v13-plugins-<sha7>
```

put the `Digest:` line in the Dockerfile's `BASE_IMAGE`, and update `runtime.yaml` `tools`
from `docker run --rm --entrypoint sh <image> -s < runtimes/kubernetes/smoke.sh` and
`dpkg-query -W`. Any change to the image is a new runtime version (below).

Local check, native architecture:

```
docker build -t rwp-runtime-kubernetes:dev runtimes/kubernetes
docker run --rm -i --entrypoint sh rwp-runtime-kubernetes:dev -s < runtimes/kubernetes/smoke.sh
python ci/check_runtime.py validate --allow-placeholder
```

## aws

Same base pin as `kubernetes` (`v13-plugins-d1c7d04`), plus the AWS CLI v2 and nothing else: the
Dockerfile downloads `awscli-exe-linux-<arch>-<version>.zip` from `awscli.amazonaws.com` at the
pinned version, checks it against the sha256 recorded in the Dockerfile (one per architecture),
installs it to `/usr/local/aws-cli` and removes `unzip` again. To move the CLI, change `AWSCLI_VERSION` and both
checksums together, bump `runtime.yaml` (`tools.aws` and `version`) and run the local check below.
`AWS_PAGER` is set empty so a task never waits on a pager. The AWS SDKs, `sam`, `cdk` and `eksctl`
are left out on purpose; an EKS kubeconfig is built by the kubernetes plugin, in `kubernetes@1`.
The versions, digests, release flow and workflow are the same as for `kubernetes` with `aws` for the name.

```
docker build -t rwp-runtime-aws:dev runtimes/aws
docker run --rm -i --entrypoint sh rwp-runtime-aws:dev -s < runtimes/aws/smoke.sh
# as the runner runs it (the smoke also makes one CLI call against a fake endpoint)
docker run --rm -i --user 65532 --read-only --tmpfs /work:rw,mode=1777 --entrypoint sh rwp-runtime-aws:dev -s < runtimes/aws/smoke.sh
python ci/check_runtime.py validate --allow-placeholder --runtime-dir runtimes/aws
## azure

The same rw-task base pin as `kubernetes`, plus the Azure CLI (`az`) and nothing else: no `aws`,
no `gcloud`. `az` is installed from PyPI into its own virtualenv (`/opt/az`,
`azure-cli==<AZURE_CLI_VERSION>`, linked to `/usr/local/bin/az`) so its dependencies cannot replace
what the SDK in `/opt/venv` imports. The install is hash-locked: `runtimes/azure/requirements.txt`
holds every dependency of `azure-cli==<AZURE_CLI_VERSION>` with the hashes of its files, the Dockerfile
installs it with `pip install --require-hashes --no-deps`, and the header of that file says how to
regenerate it. The image sets no `AZURE_CONFIG_DIR`: the `azure/cli` auth scripts create the az
session in the task's private secrets directory and the credential points `AZURE_CONFIG_DIR` at it
(the token cache lives for one task run). Telemetry and dynamic extension install are off.
To move `az`, change `AZURE_CLI_VERSION` in the Dockerfile and `requirements.in`, regenerate
`requirements.txt`, update `runtime.yaml` `tools`, and bump the runtime version.

```
docker build -t rwp-runtime-azure:dev runtimes/azure
docker run --rm -i --entrypoint sh rwp-runtime-azure:dev -s < runtimes/azure/smoke.sh
python ci/check_runtime.py validate --runtime-dir runtimes/azure --allow-placeholder
```

## Versions

`runtime.yaml` `version` is a semver; the git tag is `runtimes/kubernetes/v<version>` and must
equal it. A runtime's identity to the registry is `(name, major)`:

| Change | Bump |
| --- | --- |
| New or removed tool, new SDK major, base OS release, anything a task could notice as breaking | major |
| New tool, SDK minor | minor |
| Base or tool patch, security rebuild | patch |

Never move a published tag: the registry rejects a tag whose content changed.

## Release flow

The digests in `runtime.yaml` are the pin that reaches customer clusters, and a rebuild does not
reproduce a digest. So the image is built and pushed first, and the digests are committed before
the tag. The `runtime-kubernetes` workflow does the checks at each step.

(The `ci/check_runtime.py` commands below default to `runtimes/kubernetes`; pass
`--runtime-dir runtimes/<name>` for another runtime. The `runtime-azure` workflow does.)

1. **Change the image** (Dockerfile, base pin, `smoke.sh`) and bump `version` in `runtime.yaml`
   (the `digests` block keeps its all-zero placeholder, or the previous release's digests, for now).
   The PR builds both platforms and runs `smoke.sh` on each. The registry refuses the placeholder,
   so this state is not releasable; CI only accepts it on branches.
2. **Build on main.** The merge builds `linux/amd64` and `linux/arm64`, pushes
   `ghcr.io/runwhen-contrib/runwhen-plugins-runtime-kubernetes:main` and `:sha-<sha7>`, and writes
   the pushed `digests:` block to the run's job summary. Pushes to `v13/plugins` publish
   `:v13-plugins` and `:sha-<sha7>` the same way.
3. **PR "pin kubernetes runtime digests".** Replace the `digests` block in `runtime.yaml` with the
   one from the summary (or `python ci/check_runtime.py digests <image>@<index digest>`), and drop
   the TODO comment. A push that changes only `runtime.yaml` does not
   run the build (the PR still validates the file), so no different image is pushed: the pinned
   digest names the one already pushed, which keeps its `:sha-<sha7>` tag.
4. **Tag the merge commit** (it must be on `main`):

   ```
   git tag -a runtimes/kubernetes/vX.Y.Z -m "kubernetes runtime vX.Y.Z: <summary>"
   git push origin runtimes/kubernetes/vX.Y.Z
   ```

   The workflow checks the tag against `runtime.yaml` (`ci/check_runtime.py tag`), looks up
   `image@<pinned index digest>` in GHCR and fails unless its index lists exactly the platform
   digests `runtime.yaml` lists (`ci/check_runtime.py compare`), then adds the tag
   `image:X.Y.Z` to that image. It builds nothing: a rebuild on the tag would produce different
   digests from the ones committed.

`ci/check_runtime.py` mirrors the registry's rules (`RuntimeSpec`): `image` is a repository
without scheme, tag or digest; `digests` keys are `index`, `linux/amd64`, `linux/arm64`,
`linux/arm/v7` with `sha256:<64 hex>` values; `tools` maps strings to strings; no other keys.
The release workflow (`release.yml`, tags `*/v*`) does not run for runtime tags: they have two
slashes.
