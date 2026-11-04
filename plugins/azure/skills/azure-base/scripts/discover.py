import json
import os
import time

from azure_base import discovery as dc
from azure_base.budget import budgeted_opener, budgeted_sleep, deadline_from
from azure_base.common import read_settings
from azure_base.discovery import discover
from azure_base.errors import RwError
from azure_base.sync import SyncClient, load_credential


def main(ctx):
    deadline = deadline_from(time.monotonic())
    st = read_settings(ctx.settings)
    outputs, sync = discover(ctx, st.max_items)
    # an artifact output is a file under $RW_OUT; the script returns no value for it
    out_dir = os.environ.get("RW_OUT")
    if not out_dir:
        raise RwError("internal", "RW_OUT is not set: nowhere to write the sync artifact")
    with open(os.path.join(out_dir, "sync.json"), "w", encoding="utf-8") as out:
        json.dump(sync, out, indent=2, sort_keys=True)
        out.write("\n")

    # runwhen/sync is minted per run by the lease: a file path or inline JSON. Without it
    # (rwtask run, tests) the sync is only written to the artifact.
    credential = os.environ.get("RW_SYNC_CREDENTIAL")
    if credential:
        client = SyncClient(
            load_credential(credential),
            opener=budgeted_opener(deadline),
            sleep=budgeted_sleep(deadline),
        )
        commit, rejected = client.push(sync)
        if rejected:
            ctx.error(
                "internal",
                f"the platform rejected {len(rejected)} items: {sorted({r.get('code') for r in rejected})}",
                scope="sync",
            )
        if commit.get("status") == "held":
            ctx.error(
                "internal",
                f"the platform held the commit: {commit.get('heldReason') or 'no reason given'}",
                scope="sync",
            )
        before = outputs["summary"]
        # after the push, so what it reported is part of the summary (the commit was already sent)
        outputs["summary"] = dc.summarize(
            before["counts"],
            client.sent_partitions or sync["commit"]["partitions"],
            ctx.errors,
            before["durationMs"],
            outputs["subscription"]["subscriptionId"],
        )
    return outputs
