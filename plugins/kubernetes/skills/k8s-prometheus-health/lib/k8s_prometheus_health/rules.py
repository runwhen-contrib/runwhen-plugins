"""rules: rule groups and failing rules from /api/v1/rules."""

from k8s_prometheus_health.common import (
    age_seconds,
    cap,
    iso,
    parse_time,
    strip_urls,
    truncate,
)
from k8s_prometheus_health.errors import RwError
from k8s_prometheus_health.proxy import api_json

PATH = "/api/v1/rules"


def parse_reply(reply, what):
    """The rule groups of a proxied /api/v1/rules reply."""
    return rule_groups(api_json(reply, what), what)


def rule_groups(data, what):
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list):
        raise RwError("internal", f"{what} has no groups list")
    return groups


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def view(answers, now, limit, overdue_intervals):
    """One entry per (file, group name) across the pods that answered; the worst reading wins:
    the most unhealthy rules, the longest evaluation, the oldest last evaluation, and the
    exceeds-interval and overdue flags true if any pod says so."""
    groups, failing, exceeds, overdue, oldest, longest = {}, {}, {}, {}, {}, {}
    for _pod, doc in answers:
        for group in doc:
            key = (group.get("file"), group.get("name"))
            rules = [r for r in group.get("rules") or [] if isinstance(r, dict)]
            bad = [r for r in rules if r.get("health") != "ok" or r.get("lastError")]
            interval = number(group.get("interval"))
            took = number(group.get("evaluationTime"))
            evaluated = parse_time(group.get("lastEvaluation"))
            age = age_seconds(evaluated, now)
            row = {
                "name": key[1],
                "file": key[0],
                "interval": interval,
                "evaluationTimeSeconds": took,
                "lastEvaluation": iso(evaluated),
                "lastEvaluationAgeSeconds": age,
                "ruleCount": len(rules),
                "unhealthyRuleCount": len(bad),
                "evaluationExceedsInterval": None,
                "evaluationOverdue": None,
            }
            current = groups.get(key)
            if current is None or row["unhealthyRuleCount"] > current["unhealthyRuleCount"]:
                groups[key] = row
            if took is not None and (longest.get(key) is None or took > longest[key]):
                longest[key] = took
            if took is not None and interval:
                exceeds[key] = exceeds.get(key, False) or took > interval
            if age is not None and interval:
                overdue[key] = overdue.get(key, False) or age > overdue_intervals * interval
            if age is not None and (key not in oldest or age > oldest[key][0]):
                oldest[key] = (age, iso(evaluated))
            for rule in bad:
                rkey = key + (rule.get("name"), rule.get("type"))
                if rkey in failing:
                    continue
                error, error_truncated = truncate(strip_urls(rule.get("lastError")) or None)
                query, query_truncated = truncate(rule.get("query"))
                last = parse_time(rule.get("lastEvaluation"))
                failing[rkey] = {
                    "group": key[1],
                    "file": key[0],
                    "name": rule.get("name"),
                    "type": rule.get("type"),
                    "health": rule.get("health"),
                    "lastError": error,
                    "lastErrorTruncated": error_truncated,
                    "query": query,
                    "queryTruncated": query_truncated,
                    "evaluationTimeSeconds": number(rule.get("evaluationTime")),
                    "lastEvaluation": iso(last),
                }
    for key, row in groups.items():
        row["evaluationExceedsInterval"] = exceeds.get(key)
        row["evaluationOverdue"] = overdue.get(key)
        row["evaluationTimeSeconds"] = longest.get(key)
        if key in oldest:
            row["lastEvaluationAgeSeconds"], row["lastEvaluation"] = oldest[key]
    group_rows = sorted(
        groups.values(),
        key=lambda g: (-g["unhealthyRuleCount"], str(g["file"]), str(g["name"])),
    )
    failing_rows = sorted(
        failing.values(), key=lambda r: (str(r["file"]), str(r["group"]), str(r["name"]))
    )
    return {
        "queriedPods": [pod for pod, _ in answers],
        "groupCount": len(group_rows),
        "ruleCount": sum(g["ruleCount"] for g in group_rows),
        "unhealthyGroupCount": sum(1 for g in group_rows if g["unhealthyRuleCount"]),
        "failingRuleCount": len(failing_rows),
        "groups": cap(group_rows, limit),
        "groupsTruncated": len(group_rows) > limit,
        "failingRules": cap(failing_rows, limit),
        "failingRulesTruncated": len(failing_rows) > limit,
    }
