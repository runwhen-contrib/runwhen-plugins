---
name: prefer-read-only-kubectl
description: Investigate Kubernetes with read-only tasks before proposing any change.
---

Investigate Kubernetes problems with read-only tasks first: describe the resource, read its events and logs, and check rollout status before suggesting a change. Use the `k8s-base/kubectl` task only for get, describe, logs and top. Never read Secrets. When a change is needed, name the exact resource and the expected effect, and propose it as a write task so a person approves it.
