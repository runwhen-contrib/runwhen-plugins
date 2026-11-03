"""check-permissions: the reviews the API server is asked and the verdicts built from them."""

import json
import os
import tempfile
import time
from datetime import UTC, datetime

from runwhen_capability.contracts import permission_hash

from k8s_base import access as ax
from k8s_base.errors import RwError
from k8s_base.k8s import run_kubectl

BATCH = 100  # reviews per `kubectl create -f <List>`; one process, one API call per item
CALL_TIMEOUT = 90  # seconds per kubectl call; the task's own timeout is 300s
DEADLINE = 240  # seconds after which no further review is started
MAX_UID = 1024


class OutOfTime(Exception):
    pass


def documents(text):
    """The JSON documents `kubectl create -o json` printed, one per created object, in order."""
    decoder, pos, found = json.JSONDecoder(), 0, []
    while True:
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            return found
        doc, pos = decoder.raw_decode(text, pos)
        found.append(doc)


class Reviewer:
    """Creates review objects (nothing persists) and counts them against a budget. ctx.run has
    no stdin, so a batch goes through a file in the task's scratch directory."""

    def __init__(self, ctx, scratch, budget, deadline=DEADLINE, clock=time.monotonic):
        self.ctx, self.scratch, self.budget = ctx, scratch, budget
        self.clock, self.stop_at = clock, clock() + deadline
        self.files = 0

    def room(self):
        return self.budget

    def ask(self, items):
        """Statuses for review manifests, in order. A failed call raises RwError."""
        if len(items) > self.budget:
            raise OutOfTime()
        statuses = []
        for start in range(0, len(items), BATCH):
            batch = items[start : start + BATCH]
            left = self.stop_at - self.clock()
            if left <= 0:
                raise OutOfTime()
            name = f"review-{self.files}.json"
            self.files += 1
            with open(os.path.join(self.scratch, name), "w") as f:
                json.dump({"apiVersion": "v1", "kind": "List", "items": batch}, f)
            # --validate=false: no OpenAPI fetch, which a narrow identity may be refused and
            # which would hide the real answer behind "failed to download openapi".
            stdout, _ = run_kubectl(
                self.ctx,
                ["create", "--validate=false", "-f", name, "-o", "json"],
                cwd=self.scratch,
                timeout=min(CALL_TIMEOUT, left + 10),
            )
            docs = documents(stdout)
            if len(docs) != len(batch):
                raise RwError(
                    "internal",
                    f"kubectl returned {len(docs)} reviews for {len(batch)} asked",
                )
            self.budget -= len(batch)
            statuses += [d.get("status") or {} for d in docs]
        return statuses


def kube_system_uid(ctx):
    """The kube-system namespace UID, or None when this identity cannot read it."""
    try:
        stdout, _ = run_kubectl(
            ctx, ["get", "namespaces", "kube-system", "-o", "jsonpath={.metadata.uid}"]
        )
    except RwError as exc:
        if exc.code in ("permission_denied", "not_found"):
            return None
        raise
    uid = stdout.strip()
    return uid if 0 < len(uid) <= MAX_UID else None


def list_namespaces(ctx):
    stdout, _ = run_kubectl(ctx, ["get", "namespaces", "-o", "name"])
    return sorted(line.split("/", 1)[-1] for line in stdout.split() if line)


def check(ctx, rules, namespaces, max_reviews, max_namespaces, scratch=None):
    """The rw.access.v1 report for the rules (see CONTRACTS.md): one entry per rule, in order."""
    if not isinstance(rules, list) or len(rules) > ax.MAX_RULES:
        raise RwError("invalid_input", f"rules must be a list of at most {ax.MAX_RULES} rules")
    rules = [ax.plain(r) for r in rules]
    if any(not isinstance(r, dict) for r in rules):
        raise RwError("invalid_input", "every rule must be an object")
    wanted = ax.valid_namespaces(ax.plain(namespaces))
    if wanted is None:
        raise RwError("invalid_input", "namespaces must be a list of namespace names")
    entries = [(permission_hash(r), r, *ax.expand_rule(r)) for r in rules]
    with tempfile.TemporaryDirectory() as fallback:
        reviewer = Reviewer(ctx, scratch or os.environ.get("RW_OUT") or fallback, max_reviews)
        report = _report(ctx, entries, wanted, reviewer, max_namespaces)
    report["checkedAt"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    uid = kube_system_uid(ctx)
    if uid:
        report["root"] = {"canonicalKey": {"name": "kubeSystemUid", "value": uid}}
    return report


def _report(ctx, entries, wanted, reviewer, max_namespaces):
    # 1. cluster-wide, once for every distinct check; a rule that would overrun the budget is
    # not asked at all and comes back unverified (never dropped)
    cluster, asked, skipped = {}, [], set()
    for index, (_, _, checks, _) in enumerate(entries):
        if checks is None:
            continue
        fresh = [c for c in checks if c not in asked]
        if len(fresh) > reviewer.room() - len(asked):
            skipped.add(index)
        else:
            asked += fresh
    try:
        answers = reviewer.ask([ax.review_item(c) for c in asked])
    except OutOfTime:
        raise RwError(
            "timeout", "ran out of time before the cluster-wide reviews finished"
        ) from None
    for check_, status in zip(asked, answers):
        cluster[check_] = (bool(status.get("allowed")), status.get("reason") or "")

    # 2. namespaces, and only if some resource rule is not granted cluster-wide
    pending = [
        i
        for i, (_, _, checks, _) in enumerate(entries)
        if checks is not None
        and i not in skipped
        and checks[0][0] == ax.RESOURCE
        and not all(cluster[c][0] for c in checks)
    ]
    targets, complete, why_incomplete, per_namespace = [], True, "", {}
    if pending:
        targets, complete, why_incomplete = _targets(ctx, wanted, max_namespaces)
        refused = []
        for i in pending:
            refused += [c for c in entries[i][2] if not cluster[c][0] and c not in refused]
        complete = _probe(ctx, reviewer, targets, refused, per_namespace) and complete
        if not complete and not why_incomplete:
            why_incomplete = "some namespaces could not be checked"

    rules_out = []
    for index, (digest, rule, checks, why) in enumerate(entries):
        out = {"hash": digest, "rule": rule}
        if checks is None:
            out.update(verdict="unverified", evidence=ax._trim(f"not evaluated: {why}"))
        elif index in skipped:
            out.update(
                verdict="unverified",
                evidence="not evaluated: the maxReviews budget was used up by earlier rules",
            )
        else:
            verdict, scope, evidence = ax.verdict_for(
                checks, cluster, per_namespace, targets, complete, why_incomplete
            )
            out["verdict"] = verdict
            if scope:
                out["scope"] = scope
            out["evidence"] = evidence
        rules_out.append(out)
    return {"rules": rules_out}


def _targets(ctx, wanted, max_namespaces):
    """(namespaces to probe, whether that is all of them, why not)."""
    if wanted:
        names, complete, why = wanted, True, ""
    else:
        try:
            names = list_namespaces(ctx)
        except RwError as exc:
            if exc.code != "permission_denied":
                raise
            ctx.error(
                "permission_denied",
                "this identity cannot list namespaces, so only cluster-wide verdicts are "
                "possible; pass `namespaces` to probe named ones",
                scope="namespaces",
            )
            return [], False, "namespaces cannot be listed, so per-namespace grants are unknown"
        complete, why = True, ""
    if len(names) > max_namespaces:
        ctx.error(
            "invalid_input",
            f"{len(names)} namespaces; probing the first {max_namespaces} (setting "
            "maxNamespaces, or pass `namespaces`)",
            scope="namespaces",
        )
        names, complete = names[:max_namespaces], False
        why = f"only the first {max_namespaces} namespaces were probed"
    return names, complete, why


def _probe(ctx, reviewer, targets, refused, per_namespace):
    """Fill per_namespace[ns][check] for every refused check. True when every namespace was."""
    ok = True
    for start in range(0, len(targets), 50):
        group = targets[start : start + 50]
        try:
            statuses = reviewer.ask([ax.rules_review_item(ns) for ns in group])
        except OutOfTime:
            ctx.error(
                "timeout",
                "out of time or review budget; namespaces left unchecked",
                scope="namespaces",
            )
            return False
        except RwError as exc:
            ctx.error(exc.code, f"rules review failed: {exc.message}", scope="namespaces")
            ok = False
            continue
        for ns, status in zip(group, statuses):
            got = {c: ax.allowed_by_rules(status, c) for c in refused}
            if status.get("incomplete"):
                # the review lists only what the authorizer can enumerate: a grant it shows
                # stands, a gap must be asked as an access review
                gaps = [c for c in refused if not got[c]]
                try:
                    answers = reviewer.ask([ax.review_item(c, ns) for c in gaps])
                except OutOfTime:
                    answers = None
                except RwError as exc:
                    ctx.error(
                        exc.code,
                        f"access review failed in {ns}: {exc.message}",
                        scope="namespaces",
                    )
                    answers = None
                if answers is None:
                    got.update({c: None for c in gaps})
                    ok = False
                else:
                    got.update({c: bool(a.get("allowed")) for c, a in zip(gaps, answers)})
            per_namespace[ns] = got
    return ok
