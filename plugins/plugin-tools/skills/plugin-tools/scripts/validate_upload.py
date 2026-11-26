from plugin_tools.archive import Limits
from plugin_tools.relay import RelayError, relay_from_ctx
from plugin_tools.result import now_string
from plugin_tools.sync import Uploader
from plugin_tools.upload import MAX_UPLOAD_BYTES, run_validate
from runwhen_capability.rwerrors import RwError


def main(ctx, upload_id):
    relay = relay_from_ctx(ctx)
    try:
        data = relay.read_upload(upload_id, MAX_UPLOAD_BYTES)
    except ValueError as exc:
        raise RwError("invalid_input", str(exc)) from None
    except RelayError as exc:
        if exc.status == 404:
            raise RwError("not_found", exc.message, scope=upload_id) from None
        raise RwError("dependency", exc.message, scope=upload_id) from None
    manifest = run_validate(
        upload_id, data, Uploader(relay), listed_at=now_string(ctx.now()), limits=Limits()
    )
    return {"manifest": manifest}
