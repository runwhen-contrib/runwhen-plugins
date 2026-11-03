---
name: k8s-otelcollector-health
description: >-
  Back-pressure and data-loss facts for an OpenTelemetry Collector running as a
  Deployment, DaemonSet or StatefulSet, read from the collector's own metrics
  (Prometheus text on its metrics port, through the API server's pod proxy): each
  exporter's sending queue size against capacity per data type (traces, metrics,
  logs) and whether it is full, and the cumulative exporter send_failed and
  enqueue_failed counters, receiver refused counters and processor dropped or
  refused counters. Reads both the older and newer metric names (with or without
  the otelcol_ prefix and the _total suffix). Use when telemetry is delayed or
  missing, an exporter's destination is down, or a collector is suspected of
  dropping spans, metrics or logs.
runwhen:
  role: health
  appliesTo:
    types: [kubernetes/deployment, kubernetes/daemonset, kubernetes/statefulset]
  tags: [health, observability, opentelemetry, collector, backpressure]
---

## When to use

Traces, metrics or logs are late or missing downstream, a backend an exporter points at is down, or
a collector's memory is climbing. The target is the collector's own workload. `exporter-queue` is the
only task: the collector keeps these counters itself, so no log search is needed.

It needs `get` on the workload, `list` on its pods and `get` on `pods/proxy`. The request is
`GET /api/v1/namespaces/<ns>/pods/<pod>:<port>/proxy/metrics`, so the metrics must be reachable on the
**pod IP**: recent collector versions listen on `localhost:8888` unless `service.telemetry.metrics` is
configured with `0.0.0.0` (or `${env:MY_POD_IP}`), and then the pod answers `unreachable`; set the
`metricsPort` setting when the port is not 8888. No exec, no port-forward.

## What the task tells you

- `queues`: per pod, exporter and `dataType`, `size` against `capacity` with `utilizationPercent`,
  `atOrAboveThreshold` (the `queueUtilizationPercent` setting) and `full`. A full queue refuses or drops
  new data for that exporter. Worst first.
- `counters`: cumulative since the process started, per exporter / receiver / processor and data type:
  `send_failed` (an export attempt gave up, `errorTypes` such as `Unavailable`), `enqueue_failed` (the
  exporter's queue was full), `refused` (a receiver or processor turned data away, typically back-pressure
  from a full queue or the memory limiter) and `dropped`. `nonZeroCount` says how many are above zero.
- `pods`: every pod of the workload, `queried` or the reason it was not, and `uptimeSeconds` (how long
  the counters have been counting).

## How to read the result

Counters never reset while the process lives, so a non-zero `send_failed` may be old: compare with
`uptimeSeconds`, run the task again later and see whether the value moved, and look at whether the queue
is full now. `queues: []` is not a healthy collector: an exporter without a sending queue (the `debug`
exporter, or `sending_queue.enabled: false`) has no queue series, and an older version may not expose the
queue at all. A counter that is absent is not zero: newer collectors create a series at the first event.
`utilizationPercent: null` means the collector reports a size but no capacity (or the reverse).

A pod that is not Running, or whose page could not be read, is in `pods` with the reason and the result is
`partial` with an error per pod; when no pod could be read the task fails (`unreachable`, or
`permission_denied` if the proxy was refused, or `internal` when the page is not a collector's metrics).
`enqueue_failed` and `receiver.refused` climbing together with a full queue mean the pipeline is
saturated; `send_failed` climbing with an empty queue means the destination rejects data right away.
For a DaemonSet or a Deployment with several replicas, each pod has its own counters: compare the pods.

## What to try next

- A full queue or climbing `send_failed`: the destination. Check the exporter's endpoint and the service
  behind it (`kubernetes:k8s-workload-health/rollout-status` and `pod-status` on the backend), then
  `kubernetes:k8s-workload-logs/error-patterns` on the collector for the exporter's error lines.
- Receivers refusing with queues empty: the memory limiter or an upstream processor; look at the
  collector's memory (`kubernetes:k8s-namespace-resources/resource-usage`) and its limits.
- Pods restarting (small `uptimeSeconds`): `kubernetes:k8s-workload-health/pod-status`; an OOMKilled
  collector loses its in-memory queues.
