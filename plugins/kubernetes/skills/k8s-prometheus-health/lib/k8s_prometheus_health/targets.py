"""targets: scrape pools and unhealthy targets from /api/v1/targets?state=active."""

from k8s_prometheus_health.common import (
    age_seconds,
    cap,
    iso,
    parse_duration,
    parse_time,
    strip_urls,
    truncate,
)
from k8s_prometheus_health.errors import RwError
from k8s_prometheus_health.proxy import api_json

PATH = "/api/v1/targets?state=active"
_RANK = {"up": 0, "unknown": 1, "down": 2}


def parse_reply(reply, what):
    """The activeTargets of a proxied /api/v1/targets reply."""
    return active_targets(api_json(reply, what), what)


def active_targets(data, what):
    """The activeTargets list of a targets reply; a reply without one is an error."""
    targets = data.get("activeTargets") if isinstance(data, dict) else None
    if not isinstance(targets, list):
        raise RwError("internal", f"{what} has no activeTargets list")
    return targets


def merge(answers, now, overdue_intervals):
    """One entry per (scrapePool, scrapeUrl) across the pods that answered: replicas scrape the
    same targets and shards scrape different ones, so a target is as bad as its worst pod. Only
    the scrape URL with its query string removed is kept; discoveredLabels (which carry relabelled
    parameters such as tokens) are never read."""
    merged, seen, down = {}, {}, {}
    for pod, targets in answers:
        for target in targets:
            url = strip_urls(target.get("scrapeUrl"))
            key = (target.get("scrapePool"), url)
            health = target.get("health") if target.get("health") in _RANK else "unknown"
            labels = target.get("labels") or {}
            scraped = parse_time(target.get("lastScrape"))
            interval = parse_duration(target.get("scrapeInterval"))
            entry = merged.get(key)
            if entry is None or _RANK[health] > _RANK[entry["health"]]:
                error, error_truncated = truncate(strip_urls(target.get("lastError")) or None)
                age = age_seconds(scraped, now)
                merged[key] = {
                    "scrapePool": key[0],
                    "job": strip_urls(labels.get("job")),
                    "instance": strip_urls(labels.get("instance")),
                    "scrapeUrl": url,
                    "health": health,
                    "lastError": error,
                    "lastErrorTruncated": error_truncated,
                    "lastScrape": iso(scraped),
                    "lastScrapeAgeSeconds": age,
                    "scrapeIntervalSeconds": interval,
                    "overdue": (
                        age > overdue_intervals * interval if age is not None and interval else None
                    ),
                }
            seen.setdefault(key, set()).add(pod)
            if health == "down":
                down.setdefault(key, []).append(pod)
    for key, entry in merged.items():
        entry["seenOnPods"] = len(seen[key])
        entry["downOnPods"] = down.get(key, [])[:50]
    return merged


def view(answers, now, limit, overdue_intervals):
    merged = merge(answers, now, overdue_intervals)
    pools = {}
    for (pool, _), entry in merged.items():
        counts = pools.setdefault(pool, {"scrapePool": pool, "up": 0, "down": 0, "unknown": 0})
        counts[entry["health"]] += 1
    pool_rows = sorted(
        pools.values(), key=lambda p: (-p["down"], -p["unknown"], str(p["scrapePool"]))
    )
    for row in pool_rows:
        row["total"] = row["up"] + row["down"] + row["unknown"]
    attention = [e for e in merged.values() if e["health"] != "up" or e["overdue"]]
    attention.sort(key=lambda e: (-_RANK[e["health"]], str(e["scrapePool"]), str(e["scrapeUrl"])))
    rows = cap(attention, limit)
    return {
        "queriedPods": [pod for pod, _ in answers],
        "total": len(merged),
        "up": sum(1 for e in merged.values() if e["health"] == "up"),
        "down": sum(1 for e in merged.values() if e["health"] == "down"),
        "unknown": sum(1 for e in merged.values() if e["health"] == "unknown"),
        "overdue": sum(1 for e in merged.values() if e["overdue"]),
        "pools": cap(pool_rows, limit),
        "poolCount": len(pool_rows),
        "poolsTruncated": len(pool_rows) > limit,
        "unhealthy": rows,
        "unhealthyCount": len(attention),
        "unhealthyTruncated": len(attention) > limit,
    }
