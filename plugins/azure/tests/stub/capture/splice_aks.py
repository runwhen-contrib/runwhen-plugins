#!/usr/bin/env python3
"""Put what capture_aks.py captured into the stub's scenarios (run by capture_aks.sh).

    splice_aks.py <captured dir> <scenarios dir>

For each captured scenario: the `aks-*.json` stdout files are copied into the scenario's directory,
and its calls (calls.aks.jsonl) go into the scenario's calls.yaml between two marker comments, at
the top of the list. The stub drops `#` lines before it parses the file, so the markers are free
text; running this again replaces the block and touches nothing else in the file. A scenario that
does not exist yet gets a calls.yaml of its own (the `NEW` table below holds the comment on top)."""

import shutil
import sys
from pathlib import Path

BEGIN = "    # --- azure-aks-health: captured from the pinned az by capture/capture_aks.sh; do not edit by hand ---\n"
END = "    # --- end azure-aks-health ---\n"
HEADER = "# Format: JSON written as YAML; `<ts>` in an argv entry matches a timestamp (RFC 3339 UTC, whole seconds). See ../../README.md.\n"
NEW = {  # scenarios only azure-aks-health has: their description
    "stopped": "# stopped: aks-orders-dev is switched off (power state Stopped): the pools are stopped and Azure Monitor has no value for any interval.\n"
    "# Only the calls of azure-aks-health's cluster-status, node-pools and node-metrics.\n",
    "monitor-forbidden": "# monitor-forbidden: the identity can read aks-orders-dev but not Azure Monitor (diagnostic settings, activity log, metrics are AuthorizationFailed).\n"
    "# Only the calls of azure-aks-health's cluster-status, activity-failures and node-metrics.\n",
}


def block(captured):
    lines = (captured / "calls.aks.jsonl").read_text(encoding="utf-8")
    return BEGIN + lines + END


def main(captured, scenarios):
    captured, scenarios = Path(captured), Path(scenarios)
    for source in sorted(p for p in captured.iterdir() if p.is_dir()):
        target = scenarios / source.name
        target.mkdir(exist_ok=True)
        for stdout in source.glob("aks-*.json"):
            shutil.copyfile(stdout, target / stdout.name)
        calls = target / "calls.yaml"
        text = block(source)
        head = NEW.get(source.name)
        if head is not None or not calls.exists():
            # a scenario only azure-aks-health has: the file is entirely this capture's
            if head is None:
                raise SystemExit(f"{source.name}: no calls.yaml and no description in NEW")
            body = text.replace(BEGIN, "").replace(END, "").rstrip().rstrip(",")
            calls.write_text(f'{head}{HEADER}{{\n  "calls": [\n{body}\n  ]\n}}\n', encoding="utf-8")
            continue
        old = calls.read_text(encoding="utf-8")
        if BEGIN in old:
            start, stop = old.index(BEGIN), old.index(END) + len(END)
            new = old[:start] + text + old[stop:]
        else:
            opening = '  "calls": [\n'
            at = old.index(opening) + len(opening)
            new = old[:at] + text + old[at:]
        calls.write_text(new, encoding="utf-8")


if __name__ == "__main__":
    main(*sys.argv[1:3])
