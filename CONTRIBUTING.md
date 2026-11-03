# Contributing

Skills in this repo are re-derived from the source system (the Kubernetes API,
`psql`, a cloud CLI), not copied from classic CodeCollection bundles. The
checklist below is the review bar for every ported skill. See
[RELEASING.md](RELEASING.md) for versioning.

## Porting checklist

1. Generate the draft: `rwtask port <classic-bundle-dir> --plugin <name> --into plugins/<name>/skills/` (Task 5), then merge into the target skill per `porting/parity.yaml`.
2. For each classic task, write in one line what it asserts about the source system (from `porting/inventory/*.yaml` `asserts`). Drop tasks that assert nothing, duplicate another, or need another integration's credential (record `dropped: <reason>` in parity).
3. Re-derive from the source system: read the API object, not the classic script. Python + `ctx.run(["kubectl", ..., "-o", "json"])`; no `oc`, no `2>/dev/null`, no `|| true`, no `// 0` on command output.
4. Outputs are data, not verdicts: lists of objects with the fields an engineer would look at (name, reason, counts, timestamps), plus booleans for threshold crossings driven by settings. No severity, title or next-steps strings.
5. Thresholds and lookbacks are `settings` (admin) or `inputs` (caller) with defaults; lookback defaults ≥ 1h (classic 10m defaults missed most incidents).
6. Errors: every source-system call failure maps to a D64 code via the classifier; per-item failures use `ctx.error` and continue (→ `partial`); a failure that leaves nothing to report raises (→ `failed`).
7. `permissions`: the exact RBAC rules the script needs; target-namespace scope only unless the type is cluster-scoped. Note cluster-scoped needs in SKILL.md.
8. Fixtures: `rwtask test --record` on the kind scenarios for `healthy`, `broken` (the condition the task detects), `forbidden`, `not-found`; commit `tests/`.
9. Bounds: list outputs capped by a `maxItems` setting (default 200); raw logs and dumps go to an `artifact` output.
10. `SKILL.md`: when to use, what each task tells you, how to read the result, what to try next (naming sibling skills by `<plugin>:<skill>/<task>`).
11. Update `porting/parity.yaml` (bundle → skill/tasks, `status: ported`) and `porting/rate.csv` (bundle, start, end, minutes, classic defects found, review defects).
12. `rwtask validate plugins/<name>` and `rwtask test plugins/<name>/skills/<skill>` green before review.

## Code conventions

- Python scripts stay thin: fetch with `ctx.run`, compute in `lib/`, return outputs.
- `lib/` holds one package named after the skill in snake_case (`lib/k8s_workload_health/`), imported as `from k8s_workload_health.<module> import ...`. `lib/` is appended to `sys.path` after site-packages, so bare module names (`k8s`, `workload`) could be shadowed by an installed package.
- Logic in `lib/` is pure (parsed JSON in, outputs out) and unit-tested in `plugins/<plugin>/tests/unit/` against JSON captured from the kind scenarios (`managedFields` stripped). `tests/` at the plugin root is excluded from the archive.
