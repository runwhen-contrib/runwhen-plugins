---
name: aws-ec2-health
description: >-
  Health facts for one EC2 instance. instance-config reports its state and the
  reason it last changed, type, launch time, zone, IAM profile, network ids,
  attached EBS volumes and tags (a tag that looks like a secret is listed by key
  only; user data and key pairs are never read). instance-status reports the
  system, instance and EBS status checks and the scheduled events (reboot,
  retirement, maintenance). instance-metrics reports CloudWatch CPU, network,
  EBS and, for T-family types, CPU credit balance over a look-back window, with
  whether CPU and credits cross the thresholds; no datapoints is unknown, never
  0. Use when an instance is slow, unreachable, stopped unexpectedly or
  scheduled for retirement, or to find overused instances.
runwhen:
  role: health
  appliesTo:
    types: [aws/ec2-instance]
  tags: [health, ec2, aws, metrics, status-checks]
---

## When to use

Reach for this skill when one EC2 instance is the suspect: it is slow or unreachable, it stopped
or was terminated, EC2 has scheduled maintenance or a retirement for it, or its CPU or CPU credits
look exhausted. Start with `instance-status` for what EC2 itself says is wrong, `instance-metrics`
for the load, and `instance-config` for how it is set up. The classic bundle scanned a whole
region for instances over a CPU threshold; here the instance is the target, and
`aws:aws-base/discover` lists the instances of an account.

All three tasks only read, through the aws CLI v2 in the instance's own region (the target's
`region`, never a default), with `--output json --no-cli-pager`. They need `ec2:DescribeInstances`
(`instance-config`; `instance-metrics` reads the instance first so that a missing one is
`not_found` instead of an idle one), `ec2:DescribeInstanceStatus` (`instance-status`) and
`cloudwatch:GetMetricStatistics` (`instance-metrics`). Calls name the instance by id; the
reservation's owner is compared with the target's account, so an id from another account is
`not_found`, never read as the target's.

Never requested: the instance's user data (it often carries bootstrap scripts with credentials),
console output, key pair name or material. Tags are free text: a tag whose key names a secret
(`password`, `token`, `secret`, `api key`...) or whose value looks like one (an access key id, a
JWT, a `user:password@` URL, a PEM header, a long opaque string, even a commit sha) is listed with
its key, `value: null` and `redacted: true`. The check is cheap and not complete: do not repeat a
value you see that looks like a credential.

## What each task tells you

- `instance-config` returns `instance`: `state` (`pending`, `running`, `shutting-down`,
  `terminated`, `stopping`, `stopped`) with `stateReasonCode` and `stateReason` (for example
  `Client.UserInitiatedShutdown`, `Server.SpotInstanceTermination`,
  `Server.InsufficientInstanceCapacity`), `instanceType`, `burstable`, `launchTime`,
  `availabilityZone`, `lifecycle`, `monitoring` (`enabled` is detailed 1-minute monitoring),
  `iamInstanceProfileArn`, `vpcId`, `subnetId`, the IPs, `securityGroupIds`, `imdsTokensRequired`,
  `blockDevices` (volume id, device, status, delete on termination; at most 50 of
  `blockDeviceCount`), and `tags` (at most 50 of `tagCount`; `tagsRedacted` withheld values).
- `instance-status` returns `checks`: `systemStatus` (the AWS host), `instanceStatus` (the guest:
  OS, network config) and `attachedEbsStatus` (null in a region that does not return it), each
  `{status, details}` with `status` one of `ok`, `impaired`, `insufficient-data`,
  `not-applicable`, `initializing` and the reachability detail with `impairedSince`; the booleans
  `systemStatusImpaired`, `instanceStatusImpaired` and `attachedEbsStatusImpaired` (true for
  `impaired`, false for `ok`, null for the rest); and `events`: the scheduled events (`code`,
  `description`, `notBefore`, `notAfter`, `notBeforeDeadline`, `completed`, `canceled`, `pending`),
  `pendingEventCount` and `hasPendingEvents`.
- `instance-metrics` (input `since`, default `1h`, at most `14d`) returns `metrics`: `hasData`
  (CPUUtilization has datapoints), `cpu` (`averagePercent` weighted by sample count, `maxPercent`
  the highest period maximum), `network` (`inBytes`, `outBytes`), `ebs` (`readBytes`, `writeBytes`,
  `readOps`, `writeOps`), `cpuCredits` (`minimum` and `average` balance, `usage`; null unless the
  type is burstable), the `datapoints` of each metric, the instance `state`, `instanceType` and
  `monitoring`, and three booleans from settings: `cpuAverageAboveThreshold` and
  `cpuMaxAboveThreshold` (strictly above `cpuUtilizationThreshold`, default 80 percent) and
  `cpuCreditBalanceBelowThreshold` (the lowest balance strictly below `cpuCreditBalanceThreshold`,
  default 20 credits).

## How to read the result

- No CPUUtilization datapoints (`hasData: false`) means CloudWatch has nothing in the window: the
  instance is stopped, was launched a few minutes ago, or its metrics are not in yet (they trail
  by a few minutes). Everything that needs data is `null` and so are the booleans: that is
  "unknown", never "not busy" and never healthy. Read `instanceState` beside it. A metric without
  datapoints is null, not 0: EC2 publishes network and EBS metrics for a running instance even at
  0, so a gap is a gap. An instance with no EBS volume publishes no EBS metrics.
- Periods are 5 minutes up to a 3-day window and 1 hour above. The window ends now and is not
  aligned to the period, so the first datapoint can cover less than a full period and the last is
  still filling. With basic monitoring EC2 publishes every 5 minutes, so a short spike is averaged
  away; `monitoring: enabled` has 1-minute samples, still reported per 5-minute period.
- The metric loop stops after 90 seconds: a metric it did not reach is null with a `timeout` error
  scoped to its name, and the run is `partial`. So is a `rate_limited` or `permission_denied` metric
  call: the booleans that needed it are null.
- A stopped, stopping or terminated instance has `checksApply: false` and `not-applicable` checks:
  that is normal, not an impairment. `insufficient-data` and `initializing` are normal for the first
  minutes after a start. A terminated instance stays described for about an hour, then is
  `not_found`.
- `system-reboot`, `system-maintenance`, `instance-retirement` and `instance-stop` events are EC2
  acting on the host: `instance-retirement` means the host is failing, and an `instance-stop` with
  an EBS-backed instance is a stop, not a loss of data. A `[Completed]` or `[Canceled]` event
  (`completed`, `canceled`) is history; `hasPendingEvents` is for the rest.
- A T-family instance in standard mode with a low balance is throttled to its baseline: look at
  `cpuCredits.minimum` and `usage`. In unlimited mode it keeps its CPU and is billed for surplus
  credits, which this skill does not read.
- A run fails with a code, not an empty result, classified from the error code the CLI prints
  (`An error occurred (InvalidInstanceID.NotFound) ...`), not from the exit code alone:
  `not_found` (`InvalidInstanceID.NotFound`), `permission_denied` (`UnauthorizedOperation`,
  `AccessDenied*`, `ExpiredToken*`, `InvalidClientTokenId`, `UnrecognizedClientException`, no
  credentials), `rate_limited` (`Throttling*`, `RequestLimitExceeded`; the CLI has retried twice),
  `unreachable`, `timeout`, `invalid_input` (a malformed instance id), `dependency` (no `aws`),
  else `internal`.

## What to try next

- `instance-status` shows an impaired system check or a pending `instance-retirement`: stop and
  start the instance (not reboot) to move it to a healthy host, after checking `instance-config`
  for what is attached and whether it is EBS-backed.
- An impaired instance check: the guest is unreachable (kernel panic, full disk, a bad network
  config); the system log and screenshot are in the EC2 console and are not read here.
- `cpuMaxAboveThreshold` or `cpuCreditBalanceBelowThreshold`: widen `since` to see how long, compare
  `instanceType`, and look at the workload on it (`kubernetes:k8s-workload-health` when the
  instance is a node of a cluster you also manage, `kubernetes:k8s-node-health` for the node).
- `stateReasonCode` `Server.SpotInstanceTermination` or `Server.InsufficientInstanceCapacity`: the
  spot price or capacity, not the instance.
- Other instances in the account and region: `aws:aws-base/discover`.

## Tests

The plugin is **stub-recorded**: no ring has an AWS credential, so the fixtures were recorded with
the real runner against a stand-in for the CLI (`plugins/aws/tests/stub`, see its README) whose
canned responses follow the AWS CLI reference and are checked against the pinned CLI's own output
and whose error text is the CLI's own. They check the skill's logic and its handling of the CLI's
output, not an AWS account; they are re-recorded against a real account when a credential exists.
