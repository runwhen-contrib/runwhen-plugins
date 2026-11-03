Method used to produce these files (rw-cli-codecollection @ 49ab483)

# CNT-1 inventory brief (lane F, V13 plugins)

Read-only study of classic rw-cli-codecollection bundles. Output is one YAML file per group.
The point: RunWhen is re-deriving every classic bundle as V13 skills (SKILL.md + tasks.yaml, typed
outputs, no issues/findings). We need to know, per bundle and per task, **what it really asserts about
the source system**, what it reads, what permissions it needs, and whether it can **false-pass**
(report healthy when the command actually failed).

## Source

- Bundles: `/Users/samirpatil/Documents/runwhen-auto/envs/v13-lane-f/rw-cli-codecollection/codebundles/<bundle>/`
  (rw-cli-codecollection origin/main @ 49ab483). Read `runbook.robot`, `sli.robot`, every script it
  calls (`*.sh`, `*.py`), `.runwhen/generation-rules/*.yaml`, and the shared libraries it uses under
  `../libraries/` only as far as needed to know whether a CLI failure is caught.
- Prior audit (use, don't redo): `/Users/samirpatil/Documents/runwhen-auto/envs/codebundle-resilience-audit/ANALYSIS.md`
  and `recon/failure-handling.md`; for k8s bundles `recon/k8s-requirements.json` has per-task RBAC
  scope classes from an older commit (4d804ba) — use it as a starting point, verify against the code.
- Known facts: `RW.CLI.Run Cli` never fails on non-zero exit and runs `bash -c` without pipefail.

## False-pass definition

A task is false-pass prone when a failed/forbidden/timed-out/unreachable command can lead the task
to report "no problem". Idioms: `2>/dev/null` on the data-producing command, `|| true`, `|| echo 0`,
`|| echo "[]"`, jq `// 0` / `// []` on command output, missing `set -o pipefail` in a pipeline whose
first stage can fail, Robot keywords that ignore `${rsp.returncode}` / `stderr`, empty output treated
as healthy, SLI scoring 1 on no data. Mark:
- `confirmed`: you can point at the line where an error becomes empty/zero/healthy.
- `likely`: rc is never checked and empty output means healthy, but the exact path is indirect.
- `none`: rc checked, or empty output is reported as a problem/inconclusive.

## Output: `/Users/samirpatil/Documents/runwhen-auto/envs/v13-lane-f/inventory/<group>.yaml`

Write exactly this shape (YAML list, one entry per bundle, in alphabetical order). Be terse: one line
per field. Do not invent; if unsure, say so in `notes`.

```yaml
- bundle: k8s-deployment-healthcheck
  displayName: Kubernetes Deployment Triage          # runbook Metadata Display Name
  classicResourceTypes: [deployment]                  # generation-rules resourceTypes, verbatim
  matchHints: "pattern on name; level of detail basic"  # one line from the generation rule
  sli: true                                           # sli.robot present
  sliAsserts: "one line: what the SLI scores, and false-pass status of it"
  egress: none | "public internet: github.com clone" | "cloud API" ...
  tools: [kubectl, jq]                                # binaries the scripts need
  inputs: [NAMESPACE, DEPLOYMENT_NAME, CONTEXT, ...]  # user variables the runbook imports
  tasks:
    - title: Check Deployment Replica Status            # robot task name, variables stripped
      at: runbook.robot:962                              # file:line of the task
      asserts: "one sentence: the condition in the source system it reports as a problem"
      reads: ["kubectl get deploy <name> -o json", "kubectl get rs -l ..."]   # commands / API calls
      permissions:                                       # k8s: RBAC rules; cloud: IAM actions / az|gcloud|aws commands
        - {apiGroups: [apps], resources: [deployments, replicasets], verbs: [get, list]}
      scope: ns:target                                   # ns:target | cluster | all-namespaces | secondary-ns | subscription | resource-group | project | account | n/a
      readOnly: true                                     # false if it writes (patch, delete, rollout restart, scale, exec that mutates)
      falsePass: confirmed | likely | none
      falsePassWhy: "check_replicaset.sh:40 kubectl get rs 2>/dev/null | jq length -> 0 replicas = no issue"   # empty when none
      notes: ""                                          # bugs, hard-coded names, oc-only, duplicates another task, etc.
  bundleNotes: ""                                       # cross-task observations; obvious bugs (e.g. StatefulSet no-output)
```

Rules:
- Read-only study. Do not modify anything under rw-cli-codecollection. Write only your one YAML file.
- Validate the YAML parses: `python3 -c "import yaml,sys; d=yaml.safe_load(open(sys.argv[1])); print(len(d), sum(len(b['tasks']) for b in d))" <file>`
- Report back: the file path, the bundle count and task count printed by that command, and a
  5–10 line summary: the most common false-pass idioms, any bundle you could not finish, and any
  bundle whose classic resource type looks wrong or ambiguous.
