# plugin-tools

Registry tooling run by the platform on the platform runner (org-scope runs, `universal@1`).

## Tasks

- `sync` (runnable): lists a git source's tags, validates and compiles each new plugin version
  with the SDK, builds its deterministic archive and returns a manifest.
  - Credential: `git/https-token` only. `git/github-app` is defined in the integration but unused
    (a task cannot accept either of two credential types).
  - `maxVersions` is at most 20 (a larger value is clamped): there are 20 archive slots.
- `validate-upload` (**NOT RUNNABLE YET**): the upload's bytes are only reachable through the
  runner relay, which a task script cannot use, so the task fails with a `dependency` error. The
  validate/build code (`plugin_tools.upload.run_validate`) is kept and tested with bytes fed directly.

## Where things land in the run result

The run result is the SDK result envelope (`result-envelope`), whose `outputs` map holds:

- `outputs.manifest`: the sync manifest, schema `plugins.runwhen.com/sync-result/v1`
  (`skills/plugin-tools/schemas/sync-result.v1.json`), at most 1 MiB. Each `versions[]` entry has
  `archive: {sha256, size, files}` and `artifact: "archiveK"`.
- `outputs.archive1` .. `outputs.archive20`: `{artifact: "art_…", contentType: "application/x-tar",
  size}` for each slot written, in order; unused slots are absent. The bytes of
  `versions[i].artifact` are that artifact; their sha256 is `versions[i].archive.sha256`.

Archives travel as declared artifact outputs because a V13 script has no relay: the host uploads
`$RW_OUT/<name>` after the script exits (SDK `plugin/run.py` `_collect_artifacts`,
`plugin/lease.py` `relay_uploader`).

## Snapshot export

`PYTHONPATH=skills/plugin-tools/lib python -m plugin_tools.cli snapshot export --tags all -o snapshot.tar`
