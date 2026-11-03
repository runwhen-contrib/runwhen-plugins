# Releasing

Each plugin is released on its own. A release is a git tag
`<plugin>/vX.Y.Z` (or `<plugin>/vX.Y.Z-rc.N` for a release candidate) that
equals the `version` in `plugins/<plugin>/plugin.yaml` (CAP-13). A plugin has
one semver and its skills inherit it; skills carry no version of their own.

## Bump rules (D22)

| Change | Bump |
| --- | --- |
| New type, skill, task, or optional input | minor |
| Removed or renamed type, task, or required input; narrowed output schema | major |
| Script fix | patch |
| Permission increase | minor, flagged in the release notes |

Type names and parents never change after release, and task names change only
in a major (D21). A permission increase is flagged because it needs an admin
preview before it applies (D23).

## Steps

1. Set `version` in `plugins/<plugin>/plugin.yaml` and merge to `main`. Work lands on `v13/plugins`
   first and reaches `main` with the milestone PR; tags are cut from `main` only (the release
   workflow refuses a tag whose commit is not on `main`).
2. Tag the merge commit with an annotated tag:

   ```
   git tag -a <plugin>/vX.Y.Z -m "<plugin> vX.Y.Z: <summary>"
   git push origin <plugin>/vX.Y.Z
   ```

3. The `release` workflow checks the tag against `plugin.yaml`
   (`ci/release_check.py`), runs `rwtask validate` and `rwtask test`, builds the
   archive with `rwtask compile plugins/<plugin> -o dist/`, and attaches it to
   a GitHub release named after the tag. Tags with `-rc.N` are marked
   pre-release.

A tag whose plugin directory is missing, or whose version differs from
`plugin.yaml`, fails the workflow before anything is published.

Until `vars.SDK_REF` is set on the repo (to `contracts-v1` once lane A tags it,
`v13/plugins` before that), the validate, test, compile and release steps are
skipped: a tag only runs the tag/version check. No release is ever published
without its compiled archive.
