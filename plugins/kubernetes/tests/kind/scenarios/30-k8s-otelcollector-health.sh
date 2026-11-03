#!/usr/bin/env bash
# Sends traffic to the rw-otel collectors once they are Ready: 30 OTLP/JSON requests each of traces,
# metrics and logs to otel-ok (its queues drain into otel-sink) and to otel-broken (its queues fill:
# otel-dead has no pods). A throwaway busybox Job does it; the Job is recreated on every run, so the
# counters keep rising for a collector that was restarted. Idempotent.
set -euo pipefail
CTX="${K_CONTEXT:-kind-rw-plugins}"
K=(kubectl --context "$CTX" -n rw-otel)

"${K[@]}" rollout status deployment/otel-sink deployment/otel-ok deployment/otel-broken --timeout=300s >/dev/null

"${K[@]}" delete job otel-gen --ignore-not-found --wait=true >/dev/null
"${K[@]}" apply -f - <<'YAML' >/dev/null
apiVersion: batch/v1
kind: Job
metadata:
  name: otel-gen
  namespace: rw-otel
spec:
  backoffLimit: 2
  ttlSecondsAfterFinished: 3600
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: gen
          image: busybox:1.36
          resources:
            requests: {cpu: 1m, memory: 4Mi}
          command:
            - sh
            - -c
            - |
              set -eu
              RES='"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"rw-gen"}}]}'
              TRACE='{"resourceSpans":[{'"$RES"',"scopeSpans":[{"scope":{"name":"gen"},"spans":[{"traceId":"5b8efff798038103d269b633813fc60c","spanId":"eee19b7ec3c1b174","name":"op","kind":1,"startTimeUnixNano":"1544712660000000000","endTimeUnixNano":"1544712661000000000"}]}]}]}'
              METRIC='{"resourceMetrics":[{'"$RES"',"scopeMetrics":[{"metrics":[{"name":"rw.gen","gauge":{"dataPoints":[{"asInt":"1","timeUnixNano":"1544712660000000000"}]}}]}]}]}'
              LOG='{"resourceLogs":[{'"$RES"',"scopeLogs":[{"logRecords":[{"timeUnixNano":"1544712660000000000","body":{"stringValue":"hello"},"severityText":"INFO"}]}]}]}'
              for target in otel-ok otel-broken; do
                for i in $(seq 1 30); do
                  for kind in traces:"$TRACE" metrics:"$METRIC" logs:"$LOG"; do
                    # a full queue answers 503: expected from otel-broken, so the request is not fatal
                    wget -q -O /dev/null --header 'Content-Type: application/json' \
                      --post-data "${kind#*:}" "http://$target:4318/v1/${kind%%:*}" || true
                  done
                done
              done
YAML
"${K[@]}" wait --for=condition=complete job/otel-gen --timeout=240s >/dev/null
echo "[30-otelcollector] done"
