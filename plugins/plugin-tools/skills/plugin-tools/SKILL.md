---
name: plugin-tools
description: >-
  The plugin registry's tooling, run by the platform on the platform runner. sync lists a git
  source's plugin tags, validates and compiles each new version with the SDK, builds its
  deterministic archive, uploads it by hash and returns a manifest of versions, rejections and
  runtime images. validate-upload does the same for an org admin's offline upload (one plugin or a
  multi-plugin bundle). Not for workspace troubleshooting.
runwhen:
  tags: [plugin-registry, git]
---

## When to use

The platform runs these itself: `sync` on a schedule and on a push event for every enabled plugin
source, `validate-upload` (NOT RUNNABLE YET, see the README) when an org admin uploads a plugin. They are org-scope tasks on the
platform runner; nothing in a workspace calls them.

## What they do

Both turn a plugin file tree into a manifest entry and an uploaded archive:

1. normalize the tree (the archive rules), parse `plugin.yaml`, validate and compile it with the
   SDK's compiler and check the compiled document's schema;
2. check that the tag (or the upload's listing) agrees with `plugin.yaml`'s name and version;
3. build the deterministic ustar and take its `sha256:<hex>`;
4. write it to an artifact slot, `archive1`..`archive20` in order (output names must be declared, so
   they are fixed); the host uploads the artifacts when the task exits.

The manifest (`plugins.runwhen.com/sync-result/v1`) is capped at 1 MiB. When the next version would
not fit, or `maxVersions` is reached, `more` is `true` and the next run continues; tags registered
by then arrive in `known` and are skipped.

## Hardening

Every git call (one helper, `plugin_tools/gitrun.py`) runs with `-c core.hooksPath=/dev/null -c
protocol.allow=never -c protocol.https.allow=always`, `GIT_CONFIG_GLOBAL=/dev/null`,
`GIT_CONFIG_NOSYSTEM=1`, `GIT_TERMINAL_PROMPT=0`, and a per-run `HOME` and work directory that are
deleted when the run ends. The credential reaches git only through `GIT_ASKPASS` and only for the
calls that contact the remote. The source URL must be `https://` without user information.

## Release snapshot

`python -m plugin_tools.cli snapshot export` (see `lib/plugin_tools/cli.py`) builds the air-gap
snapshot tar from this repository's tags for CI.
