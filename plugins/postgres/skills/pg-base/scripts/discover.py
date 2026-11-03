import json
import os

from pg_base.discovery import discover
from pg_base.errors import RwError
from pg_base.settings import read_settings


def main(ctx):
    st = read_settings(ctx.settings)
    outputs, sync = discover(ctx, st.max_items)
    # an artifact output is a file under $RW_OUT; the script returns no value for it
    out_dir = os.environ.get("RW_OUT")
    if not out_dir:
        raise RwError("internal", "RW_OUT is not set: nowhere to write the sync artifact")
    with open(os.path.join(out_dir, "sync.json"), "w", encoding="utf-8") as out:
        json.dump(sync, out, indent=2, sort_keys=True)
        out.write("\n")
    return outputs
