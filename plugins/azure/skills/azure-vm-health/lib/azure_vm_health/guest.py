"""Reading what a guest script printed: the run-command reply, then the RW_BEGIN ... RW_END block.

`az vm run-command invoke` answers `{"value": [{"code", "displayStatus", "level", "message"}]}`
where `message` is the text `Enable succeeded: \\n[stdout]\\n...\\n[stderr]\\n...`. A reply that does
not hold the block its script prints (cut short, another command's output) is an error, never a
clean reading: the script prints RW_END only after its one command ran, with that command's exit
status."""

import re

from .common import NESTED_LIMIT, TEXT_LIMIT, listing
from .errors import RwError
from .text import safe_clip

_STDOUT = re.compile(r"\[stdout\]\n(.*?)(?:\n\[stderr\]\n(.*))?\Z", re.S)
_KEY = re.compile(r"^([a-z_]+)=(.*)$")


def split_message(message):
    """(stdout, stderr) of a run-command message."""
    found = _STDOUT.search(str(message or ""))
    if not found:
        return "", str(message or "")
    return found.group(1), found.group(2) or ""


def block(check, stdout):
    """The lines between RW_BEGIN <check> and RW_END <check>, and the exit status RW_END carries."""
    lines = stdout.splitlines()
    begin = f"RW_BEGIN {check}"
    end = re.compile(rf"^RW_END {re.escape(check)} rc=(\d+)$")
    try:
        start = lines.index(begin)
    except ValueError:
        raise RwError("internal", f"the {check} script printed no {begin} line") from None
    for i in range(start + 1, len(lines)):
        found = end.match(lines[i])
        if found:
            return lines[start + 1 : i], int(found.group(1))
    raise RwError(
        "internal", f"the {check} script printed no RW_END line: its output was cut short"
    )


def invocation(reply):
    """(code, displayStatus, level, stdout, stderr) of the first entry of a run-command reply."""
    values = reply.get("value") if isinstance(reply, dict) else None
    if not isinstance(values, list) or not values or not isinstance(values[0], dict):
        raise RwError("internal", "az vm run-command invoke returned no result")
    entry = values[0]
    stdout, stderr = split_message(entry.get("message"))
    return (
        str(entry.get("code") or ""),
        entry.get("displayStatus") or None,
        entry.get("level") or None,
        stdout,
        stderr,
    )


def _int(text, name):
    try:
        return int(text)
    except (TypeError, ValueError):
        raise RwError("internal", f"the guest printed a {name} that is not a number") from None


def _num(text, name):
    try:
        return float(text)
    except (TypeError, ValueError):
        raise RwError("internal", f"the guest printed a {name} that is not a number") from None


def _error_text(lines):
    """The text after `error=` in a script's output (and the lines that continue it), or None."""
    text = []
    for line in lines:
        if line.startswith("error="):
            text = [line[len("error=") :]]
        elif text:
            text.append(line)
    return safe_clip(" ".join(text))[0] if text else None


def disk(lines, rc, threshold, limit):
    """Filesystems from `df -P -k -l`, as the script prints them: the header, at most 40 rows sorted by
    Use% and `rows_total=` with the number of rows before that cut."""
    error = _error_text(lines)
    if rc != 0 or error:
        raise RwError("internal", f"df failed in the guest (exit {rc}): {error or 'no message'}")
    if not lines or not lines[0].startswith("Filesystem"):
        raise RwError("internal", "the guest's df printed no header line")
    total = next((line for line in lines if line.startswith("rows_total=")), None)
    if total is None:
        raise RwError("internal", "the disk script printed no rows_total line: output cut short")
    rows_total = _int(total.split("=", 1)[1].strip(), "row count")
    rows = []
    for line in lines[1:]:
        if line.startswith("rows_total="):
            continue
        parts = line.split(None, 5)
        if len(parts) < 6:
            raise RwError("internal", "the guest's df printed a line with too few columns")
        used, avail = _int(parts[2], "disk usage"), _int(parts[3], "disk usage")
        total_kb = used + avail
        percent = round(used / total_kb * 100, 1) if total_kb else None
        rows.append(
            {
                "filesystem": parts[0][:256],
                "mount": parts[5][:256],
                "sizeKb": _int(parts[1], "disk size"),
                "usedKb": used,
                "availableKb": avail,
                "usedPercent": percent,
                "overThreshold": (percent > threshold) if percent is not None else None,
            }
        )
    rows.sort(key=lambda r: -(r["usedPercent"] if r["usedPercent"] is not None else -1))
    return {
        "threshold": threshold,
        "filesystems": listing(rows, limit),
        "rowsTotal": rows_total,
        # the script keeps the 40 fullest rows: more than that and the others were not read
        "cutInGuest": rows_total > len(rows),
        "overThreshold": any(r["overThreshold"] for r in rows),
    }


def memory(lines, rc, threshold):
    """Memory from /proc/meminfo: used is total minus MemAvailable, so reclaimable cache is not used."""
    if rc != 0:
        raise RwError("internal", f"reading /proc/meminfo exited {rc} in the guest")
    values = {}
    for line in lines:
        found = re.match(r"^(\w+):\s+(\d+)\s*kB$", line.strip())
        if found:
            values[found.group(1)] = int(found.group(2))
    if "MemTotal" not in values:
        raise RwError("internal", "the guest's /proc/meminfo has no MemTotal")
    total, avail = values["MemTotal"], values.get("MemAvailable")
    percent = round((total - avail) / total * 100, 1) if avail is not None and total else None
    return {
        "threshold": threshold,
        "totalKb": total,
        "availableKb": avail,
        "freeKb": values.get("MemFree"),
        "cachedKb": values.get("Cached"),
        "swapTotalKb": values.get("SwapTotal"),
        "swapFreeKb": values.get("SwapFree"),
        "usedPercent": percent,
        # a kernel without MemAvailable (before 3.14) gives no figure: unknown, never "under"
        "overThreshold": (percent > threshold) if percent is not None else None,
    }


def uptime(lines, rc, threshold_days):
    if rc != 0:
        raise RwError("internal", f"reading /proc/uptime exited {rc} in the guest")
    if len(lines) < 2:
        raise RwError("internal", "the guest printed no /proc/uptime and /proc/loadavg lines")
    seconds = _num(lines[0].split()[0], "uptime")
    load = lines[1].split()
    if len(load) < 3:
        raise RwError("internal", "the guest's /proc/loadavg has too few fields")
    days = round(seconds / 86400, 2)
    return {
        "threshold": threshold_days,
        "seconds": round(seconds),
        "days": days,
        "overThreshold": days > threshold_days,
        "load": {
            "one": _num(load[0], "load average"),
            "five": _num(load[1], "load average"),
            "fifteen": _num(load[2], "load average"),
        },
    }


def patches(lines, rc):
    facts = {}
    packages = []
    error = []
    current = None
    for line in lines:
        found = _KEY.match(line)
        if found:
            current = found.group(1)
            if current == "package":
                packages.append(found.group(2)[:128])
            elif current == "error":
                error = [found.group(2)]
            else:
                facts[current] = found.group(2)
        elif current == "error":
            error.append(line)
    if rc != 0 or error:
        text, _ = safe_clip(" ".join(error) or f"the package manager exited {rc}")
        raise RwError("internal", f"the guest's package manager failed: {text}")
    manager = facts.get("manager")
    if manager is None:
        raise RwError("internal", "the patches script printed no manager line")

    def number(name):
        value = facts.get(name)
        return _int(value, name) if value not in (None, "unknown") else None

    reboot = facts.get("reboot_required")
    return {
        "packageManager": manager,
        # no known package manager is "could not tell", not "up to date"
        "pending": number("pending"),
        "security": number("security"),
        "heldBack": number("kept_back"),
        "packages": packages[:NESTED_LIMIT],
        "indexAgeSeconds": number("index_age_seconds"),
        "rebootRequired": {"yes": True, "no": False}.get(reboot),
    }


def stderr_text(text):
    cut, truncated = safe_clip(text.strip(), TEXT_LIMIT)
    return cut, truncated
