# runwhen-plugins

The V13 content repo. Each plugin bundles one integration (its resource types,
facets and credential shapes, data only) with the skills that target it, and
each skill holds the tasks that do the work: plugin -> integration -> skills ->
tasks. All plugins live in this one repo; each is versioned and released
independently with its own `<plugin>/vX.Y.Z` tag.

## Layout

```
plugins/
  <plugin>/                  kubernetes, postgres, azure, aws, gcp, misc
    plugin.yaml              name, version, description, requires
    integration/             data only: types, facets, dependencies, schemas
    skills/
      <skill>/
        SKILL.md
        tasks.yaml
        scripts/ lib/ schemas/ references/ assets/ tests/
ci/                          repo tooling (changed-plugin selection, tag checks) and its tests
runtimes/                    runtime images plugin tasks run in (kubernetes@1, aws@1), with their runtime.yaml
porting/                     classic-bundle inventory and parity tracking
.github/workflows/           per-plugin CI and release
```

## Working here

- [CONTRIBUTING.md](CONTRIBUTING.md): the porting checklist every ported skill follows.
- [RELEASING.md](RELEASING.md): version bump rules and per-plugin tags.
- [runtimes/README.md](runtimes/README.md): the runtime images, their `runtime.yaml` and release flow.
