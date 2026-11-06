"""check-permissions: the reviews the API server is asked and the verdicts built from them."""

import json
import os
import tempfile
import time

from runwhen_capability.contracts import permission_hash

from k8s_base import access as ax
from k8s_base.errors import RwError
from k8s_base.k8s import run_kubectl

BATCH = 100  # reviews per `kubectl create -f <List>`; one process, one API call per item
CALL_TIMEOUT = 90  # seconds per review call
SIDE_TIMEOUT = 20  # seconds per other call (uid, api-resources, namespace list): at most 3
REVIEW_DEADLINE = 180  # no review starts later; with the 3 side calls the run ends by 240s
MAX_UID = 1024


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

    def __init__(self, ctx, scratch, budget, deadline=REVIEW_DEADLINE, clock=time.monotonic):
        self.ctx, self.scratch, self.budget = ctx, scratch, budget
        self.clock, self.stop_at = clock, clock() + deadline
        self.files = 0
        self.reported = set()

    def _note(self, key, code, message, scope):
        if key not in self.reported:
            self.reported.add(key)
            self.ctx.error(code, message, scope=scope)

    def ask(self, items, scope):
        """[(status, why)] for review manifests, in order. status is None, with the reason, for a
        review that was not answered: its batch failed (reported as ctx.error), the budget or the
        time ran out. Nothing raises, so one bad batch costs only the rules it carried."""
        answers = []
        for start in range(0, len(items), BATCH):
            batch = items[start : start + BATCH]
            left = self.stop_at - self.clock()
            if left <= 0:
                self._note(
                    "time", "timeout", "out of time; the remaining reviews were not made", scope
                )
                answers += [(None, "ran out of time")] * len(batch)
                continue
            fit = batch[: max(self.budget, 0)]
            if fit:
                self.budget -= len(fit)
                try:
                    answers += [(s, "") for s in self._create(fit, min(CALL_TIMEOUT, left))]
                except RwError as exc:
                    self._note(
                        ("batch", self.files),
                        exc.code,
                        f"a batch of {len(fit)} reviews failed: {exc.message}",
                        scope,
                    )
                    answers += [(None, f"the review failed ({exc.code})")] * len(fit)
            if len(fit) < len(batch):
                self._note(
                    "budget",
                    "invalid_input",
                    "setting maxReviews was used up; the remaining reviews were not made",
                    scope,
                )
                answers += [(None, "the maxReviews budget was used up")] * (len(batch) - len(fit))
        return answers

    def _create(self, batch, timeout):
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
            timeout=timeout,
        )
        docs = documents(stdout)
        if len(docs) != len(batch):
            raise RwError(
                "internal", f"kubectl returned {len(docs)} reviews for {len(batch)} asked"
            )
        return [d.get("status") or {} for d in docs]


def kube_system_uid(ctx):
    """The kube-system namespace UID, or None when this identity cannot read it."""
    try:
        stdout, _ = run_kubectl(
            ctx,
            ["get", "namespaces", "kube-system", "-o", "jsonpath={.metadata.uid}"],
            timeout=SIDE_TIMEOUT,
        )
    except RwError as exc:
        if exc.code in ("permission_denied", "not_found"):
            return None
        raise
    uid = stdout.strip()
    return uid if 0 < len(uid) <= MAX_UID else None


def list_namespaces(ctx):
    stdout, _ = run_kubectl(ctx, ["get", "namespaces", "-o", "name"], timeout=SIDE_TIMEOUT)
    return sorted(line.split("/", 1)[-1] for line in stdout.split() if line)


def cluster_scoped(ctx):
    """(set of cluster-scoped (group, resource), None), or (None, the RwError) when
    api-resources failed; the static table in access.py then still answers for the common kinds."""
    try:
        stdout, _ = run_kubectl(
            ctx, ["api-resources", "--namespaced=false", "-o", "name"], timeout=SIDE_TIMEOUT
        )
    except RwError as exc:
        return None, exc
    return ax.parse_cluster_scoped(stdout), None


def check(ctx, rules, namespaces, max_reviews, max_namespaces, scratch=None):
    """The rw.access.v1 report for the rules (see CONTRACTS.md): one entry per rule, in order."""
    if not isinstance(rules, list) or len(rules) > ax.MAX_RULES:
        raise RwError("invalid_input", f"rules must be a list of at most {ax.MAX_RULES} rules")
    rules = [ax.plain(r) for r in rules]
    if any(not isinstance(r, dict) or not r for r in rules):
        raise RwError("invalid_input", "every rule must be a non-empty object")
    wanted = ax.valid_namespaces(ax.plain(namespaces))
    if wanted is None:
        raise RwError("invalid_input", "namespaces must be a list of namespace names")
    try:
        entries = [(permission_hash(r), r, *ax.expand_rule(r)) for r in rules]
    except ValueError as exc:
        raise RwError("invalid_input", f"a rule cannot be hashed: {exc}") from None
    with tempfile.TemporaryDirectory() as fallback:
        reviewer = Reviewer(ctx, scratch or os.environ.get("RW_OUT") or fallback, max_reviews)
        report = _report(ctx, entries, wanted, reviewer, max_namespaces)
    report["checkedAt"] = ctx.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    uid = kube_system_uid(ctx)
    if uid:
        report["root"] = {"canonicalKey": {"name": "kubeSystemUid", "value": uid}}
    return report


def _cluster_wide(reviewer, entries):
    """{check: (allowed, text)}, one review for every distinct check of every evaluable rule.
    allowed is None when the review was not answered or the server could not evaluate it."""
    asked = []
    for _, _, checks, _ in entries:
        asked += [c for c in checks or () if c not in asked]
    answers = reviewer.ask([ax.review_item(c) for c in asked], "reviews")
    cluster = {}
    for c, (status, why) in zip(asked, answers):
        cluster[c] = _answer(status, why)
    return cluster


def _answer(status, why):
    if status is None:
        return None, why
    if status.get("allowed"):
        return True, status.get("reason") or ""
    if status.get("evaluationError"):
        return None, f"the server could not evaluate it: {status['evaluationError']}"
    return False, status.get("reason") or ""


def _report(ctx, entries, wanted, reviewer, max_namespaces):
    named_namespace = ax.is_named_namespace_get
    cluster = _cluster_wide(reviewer, entries)
    live = [(i, e[2]) for i, e in enumerate(entries) if e[2] is not None]

    # which refused expansions are cluster-scoped, from api-resources, backed by a static table
    refused = {c for _, cs in live for c in cs if c[0] == ax.RESOURCE and cluster[c][0] is not True}
    discovered, discovery_error = (None, None)
    if refused:
        discovered, discovery_error = cluster_scoped(ctx)
    scopes = {c: ax.scope_of(c, discovered) for c in refused}

    # rules a cluster-scoped refusal already denies need no namespaces; the rest do
    def decided(cs):
        # denied by a cluster-scoped refusal, or cannot be told for want of an answer
        return any(
            (scopes.get(c) == ax.CLUSTER and cluster[c][0] is False and not named_namespace(c))
            or cluster[c][0] is None
            for c in cs
        )

    probing = [
        c
        for _, cs in live
        if not decided(cs)
        for c in cs
        if c in refused and (scopes[c] != ax.CLUSTER or named_namespace(c))
    ]
    targets, complete, why_incomplete, per_namespace = [], True, "", {}
    if probing:
        targets, complete, why_incomplete = _targets(ctx, wanted, max_namespaces)
        for c in set(probing):
            if named_namespace(c):
                # a namespace named in the rule is its own scope, if it is one we look in
                scopes[c] = ax.NAMESPACED if c[5] in targets else ax.CLUSTER
        asked = list(dict.fromkeys(c for c in probing if scopes[c] != ax.CLUSTER))
        complete = _probe(ctx, reviewer, targets, asked, per_namespace) and complete
        if not complete and not why_incomplete:
            why_incomplete = "some namespaces could not be checked"
    if discovery_error and any(scopes[c] is None for c in probing):
        ctx.error(
            discovery_error.code,
            f"api-resources failed, so whether some kinds are cluster-scoped is unknown: "
            f"{discovery_error.message}",
            scope="discovery",
        )

    rules_out = []
    for digest, rule, checks, why in entries:
        out = {"hash": digest, "rule": rule}
        if checks is None:
            out.update(verdict="unverified", evidence=ax._trim(f"not evaluated: {why}"))
        else:
            verdict, scope, evidence = ax.verdict_for(
                checks, cluster, per_namespace, targets, complete, why_incomplete, scopes
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
        answers = reviewer.ask([ax.rules_review_item(ns) for ns in group], "namespaces")
        for ns, (status, _) in zip(group, answers):
            if status is None:
                ok = False  # the batch error is already reported; the namespace stays unprobed
                continue
            got = {c: ax.allowed_by_rules(status, c) for c in refused}
            if status.get("incomplete") or status.get("evaluationError"):
                # the review lists only what the authorizer can enumerate: a grant it shows
                # stands, a gap must be asked as an access review
                gaps = [c for c in refused if not got[c]]
                replies = reviewer.ask([ax.review_item(c, ns) for c in gaps], "namespaces")
                for c, (reply, why) in zip(gaps, replies):
                    got[c] = _answer(reply, why)[0]
                ok = ok and all(r is not None for r, _ in replies)
            per_namespace[ns] = got
    return ok
