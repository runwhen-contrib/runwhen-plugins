# CNT-3 pilot: measured porting rate (2026-11-03)

The pilot ported 15 classic rw-cli kubernetes bundles (88 classic tasks) into 7 V13 skills (31 typed tasks). Every
skill was written by one Sonnet agent from `rwtask port` drafts and the CNT-1 inventory, reviewed by a separate Sonnet
agent, fixed, verified under the real SDK runner (`rwtask run`) on kind, and given recorded `rwtask test` fixtures.
Raw log: [`rate.csv`](rate.csv). Process: [`CONTRIBUTING.md`](../CONTRIBUTING.md).

## The number

| | Agent minutes | Share |
|---|---|---|
| Port (draft → skill, unit tests, kind smoke) | 89 | 37% |
| Independent review | 49 | 20% |
| Review fixes, incl. real-runner fixes | 55 | 23% |
| Recorded fixtures (healthy/broken/forbidden/not-found) | 49 | 20% |
| **Total** | **≈ 242** | |

- **≈ 16 agent-minutes per classic bundle, ≈ 2.75 per classic task, ≈ 8 per V13 task**, all-in (port, review, fixes,
  fixtures). Orchestration on the main loop (briefs, rebases, PRs, verification) comes on top and was not timed.
- **Wall clock:** first port started 18:05Z, the last pilot PR opened about 19:25Z, so about 80 minutes for 15 bundles
  with up to 7 agents in parallel. Fixtures for 6 skills took a further ~15 minutes (3 at a time).
- **Against the plan's estimate** (0.5–2 engineer-days per classic task, so 40–170 days for the 84-task basic set):
  agents do the 88 tasks of this pilot in about 4 agent-hours.

| Skill | Bundles | Classic tasks → V13 tasks | Agent min | Classic defects not reproduced | Review findings | Fixture scenarios |
|---|---|---|---|---|---|---|
| k8s-workload-health | 4 | 31 → 7 | 40 | 11 | 12 | 31 |
| k8s-namespace-health | 2 (1 partial) | 13 → 5 | 31 | 11 | 10 | 20 |
| k8s-workload-logs | 3 (+ log tasks of 3) | 14 → 3 | 43 | 9 | 17 | 15 |
| k8s-pvc-health | 1 | 6 → 4 | 28 | 10 | 7 | 17 |
| k8s-node-health | 2 | 4 → 4 | 26 | 9 | 11 | 16 |
| k8s-ingress-health | 2 | 7 → 3 | 25 | 10 | 8 | 13 |
| k8s-workload-ops (write) | 1 | 13 → 5 | ≈ 49 | 10 | 6 (1 high) | pending |

## Defects

- **Classic defects not reproduced: 70** across 15 bundles (≈ 4.7 per bundle). They are mostly false-passes: a failed,
  forbidden or timed-out command read as healthy. Others are checks that can never fire or always fire, and
  StatefulSet tasks that produce no output.
- **Review findings on the ported skills: 71** (≈ 10 per skill). No false-pass survived review. The one high finding
  was in the write skill: under the real runner a slow rollout failed the task after the restart was applied.
- **Bugs only the real runner or the recording found: 4.** Object settings arrive as a Mapping with snake-cased keys.
  An artifact is a file in `$RW_OUT`. `ctx.run` raises on a non-zero exit. The SDK classifies kubectl's
  "executable file not found" as `not_found`. Since then a real-runner smoke is mandatory for every skill.

## What changed the rate (carry into M2)

1. **Rules up front.** Pilots 2–4 started before the review rules (brief step 3b) existed, and each spent 3–6 minutes
   reworking. Pilots 5–7 started with them and needed less rework (node 4.5 min). Every M2 agent gets the final brief.
2. **Real runner from the first smoke.** The stand-in ctx hid four SDK behaviours. With `rwtask run` and
   `runwhen_capability.testing` (lane A) available from M2 on, the stand-in goes.
3. **SDK classifier instead of per-skill copies.** Each skill copied a stderr classifier, and reviewers found the same
   gaps in each copy. The M2 sweep moves every skill to the SDK's `RwCommandError` codes (lane A has merged the five
   kubectl rules).
4. **Shared kind cluster contention.** Skills that add scenario objects to the shared `rw-broken` namespace interfere
   with each other: a quota blocked other skills' pods, and a write-skill smoke changed a fixture target. M2 skills put
   their scenario objects in their own namespace (`rw-<skill>`) through `scenarios/30-<skill>.yaml`.
5. **Review is worth its 20%.** Every skill had real findings, including RBAC that missed a verb `kubectl` silently
   needs (`pods get` before `logs` and `exec`). A test now maps each task's kubectl argv to RBAC and compares it with
   the declared permissions.

## M2 fan-out

The rest of the kubernetes parity: 31 bundles become 20 skills (`porting/parity.yaml`), plus `k8s-base` (INT-8:
discover, kubectl, check-permissions).

- At ≈ 35 agent-minutes per skill that is about 12 agent-hours of skill work.
- Six concurrent implementers, each with a dedicated reviewer: about 2–2.5 hours of wall clock per wave of 6 skills,
  so about 4 waves. Kind contention, not tokens, is the limit (one node, 4 GiB Docker VM).
- `k8s-base` and the postgres plugin go to one agent each, outside the waves, because they are design-heavy (discovery
  sync, permission verdicts, a new integration).
