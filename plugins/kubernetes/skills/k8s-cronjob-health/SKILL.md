---
name: k8s-cronjob-health
description: >-
  Health facts for a Kubernetes CronJob: its schedule and time zone, whether it is
  suspended, lastScheduleTime and lastSuccessfulTime, when the schedule last should
  have fired and whether scheduled runs were missed (starting deadline, Forbid
  concurrency blocked by a still-running Job), its events, and the Jobs it
  started recently with how each ended (succeeded, failed with the Failed condition,
  still active), how long it took and how its pods' containers terminated. Use when a
  CronJob did not run, runs late or not at all, keeps failing, never succeeds, or a
  batch result is missing.
runwhen:
  role: health
  appliesTo:
    types: [kubernetes/cronjob]
  tags: [health, cronjob, jobs, schedule]
---

## When to use

The target is a CronJob and a scheduled piece of work did not happen, did not finish,
or keeps failing. Start with `schedule-health`: it says whether the CronJob is expected
to run at all (suspended?), whether the controller started the runs it should have, and
whether a still-running Job is holding the next one back. Then `recent-runs` for how the
Jobs it did start ended. For every failed Job in the whole namespace, not one CronJob,
use `kubernetes:k8s-namespace-health/failed-jobs`.

Both tasks only read, inside the CronJob's namespace: `get` on the CronJob, `list` on
Jobs, Events (`schedule-health`) and Pods (`recent-runs`). No task reads Secrets,
ConfigMaps or logs.

## What each task tells you

- `schedule-health`:
  - `schedule`: the CronJob's own fields (`suspended`, `schedule`, `timeZone`,
    `concurrencyPolicy`, `startingDeadlineSeconds`, `lastScheduleTime`,
    `lastSuccessfulTime`, `activeCount`) and what the schedule implies. `lastExpectedFire`
    is the most recent time the schedule should have fired (null if it has not fired since
    the CronJob was created); `nextExpectedFire` the next. `neverFires` is true when the
    schedule has no next fire at all (`0 0 31 2 *`), with an `invalid_input` error scoped
    `schedule`. `lastScheduleTime` is the
    *scheduled* time of the last Job the controller created, so scheduled times after it
    are runs that were never started: `missedFires` counts them (stops at 101,
    `missedFiresCapped`), `behindSchedule` is true when there is at least one that is more
    than `lateGraceSeconds` (default 60) old, and `missedSince` when one of them falls in
    the last `since` (default 1h). `deadlineExceeded` (only with `startingDeadlineSeconds`)
    is true when the latest missed fire (one already past the grace period) is older than
    that deadline (or, for a deadline shorter than the grace period, past the deadline), so
    the controller will skip it for good. `forbidBlocked` is true for `concurrencyPolicy:
    Forbid` with an active Job and missed fires. `successOlderThanLastSchedule` is true
    when the last success is older than the last scheduled run (true while that run is
    still going or failed).
  - `activeJobs`: Jobs in `status.active` with start time, `runningSeconds` and pod counts;
    `exists: false` is a listed Job that has been deleted. A `suspended` Job (`spec.suspend`)
    holds the `Forbid` slot without running anything: its `runningSeconds` is null.
  - `events`: events about the CronJob last seen in `since`, each with its `type`:
    `JobAlreadyActive` (a **Normal** event: Forbid skipped a run), and the warnings
    `MissSchedule`, `UnexpectedJob`, `TooManyMissedTimes`, `InvalidSchedule`,
    `UnparseableSchedule`, `UnknownTimeZone`, `UnsupportedSchedule`, `FailedCreate`,
    `MissingJob`. The routine `SuccessfulCreate`, `SawCompletedJob` and
    `SuccessfulDelete` events are left out; the Jobs themselves say the same.
- `recent-runs`: Jobs owned by the CronJob (by `ownerReferences`) whose start is within
  `since` (default 24h), newest first. Per run: `status` (`succeeded`, `failed`, `active`: not finished yet, so also pending and
  suspended Jobs, see `suspended`),
  `scheduledTime`, `startTime`, `finishedAt` and `durationSeconds` (active runs have
  `runningSeconds` instead, null while suspended), `failureReason`/`failureMessage` from the Failed condition
  (`BackoffLimitExceeded`, `DeadlineExceeded`), pod counts, and `pods` with each
  container's `state`, `waitingReason` and `lastTermination` (`reason` such as `Error` or
  `OOMKilled`, `exitCode`; init containers have `init: true`). `counts` totals the window
  and also reports `owned` Jobs, `latestStatus` and `activeBeforeWindow` outside it.

## How to read the result

Outputs are facts, not verdicts: nothing here says healthy or unhealthy.

- Check `suspended` first. A suspended CronJob expects nothing: `missedFires` is 0 and
  `behindSchedule` false whatever `lastScheduleTime` says, and `nextExpectedFire` is null.
- An empty `runs` with `counts.owned` above 0 means the Jobs are all older than `since`;
  with `owned` 0 the CronJob has no Job left (never ran, or its history limits removed
  them: `successfulJobsHistoryLimit` defaults to 3, `failedJobsHistoryLimit` to 1). An
  empty `activeJobs` means no Job is running. An empty `events` is not proof of calm:
  Kubernetes keeps events for about an hour by default.
- `schedule`'s evaluated fields are null when the schedule could not be evaluated (an
  error scoped `schedule`, e.g. the time zone database lacks the zone: evaluating a
  `timeZone` needs tzdata in the runtime image, and a missing zone is a `dependency`
  error). Without
  `timeZone` the controller uses its own zone, normally UTC, and so does this task
  (`evaluatedTimeZone`).
- Lists stop at `maxItems` (default 200), nested lists at 50 and message text at 1024
  characters (`truncated`). Job history is bounded by the CronJob's history limits.
- A secondary list that fails (Jobs, Events, Pods) is an error scoped to it
  (`jobs.batch`, `events`, `pods`) with the primary facts kept: `events` or a run's `pods`
  is then null, and active Jobs lose their details; null is "not read", never "empty".
  A CronJob that does not exist, or one the identity cannot read, fails the task.
- Normal states that look like failures:
  - `lastSuccessfulTime` null on a CronJob that has never succeeded yet, or on one from
    before Kubernetes 1.21 semantics: use `recent-runs`;
  - a failed run with the CronJob still in `Forbid` or `Replace` mode: the history limit
    keeps the last failed Job until it is replaced;
  - `behindSchedule` for a few minutes after the controller was down or the cluster was
    scaled to zero; with `startingDeadlineSeconds` set a missed window stays missed until
    the next fire is started in time;
  - `FailureTarget` and `SuccessCriteriaMet` are transient Job conditions on the way to
    Failed or Complete; a run with only those is still `active`;
  - Jobs created by hand with `kubectl create job --from=cronjob/...` also appear in
    `recent-runs` (same owner reference) and have no `scheduledTime` annotation.

## What to try next

- A failed run: `kubernetes:k8s-workload-logs/recent-logs` on the Job's pod for what it
  printed; the pod's `lastTermination` says how it ended (`OOMKilled`, `Error`).
- `Forbid` holding runs back: the active Job in `activeJobs` is the one to inspect; a Job
  that never finishes is a workload problem, not a schedule one.
- Jobs failing across the namespace, or the Job's pods Pending/ImagePullBackOff:
  `kubernetes:k8s-namespace-health/failed-jobs`, `kubernetes:k8s-namespace-health/unhealthy-pods`
  and `kubernetes:k8s-namespace-health/warning-events`.
- Suspension, schedule or deadline changes are changes to the CronJob; this skill does not
  make them.
