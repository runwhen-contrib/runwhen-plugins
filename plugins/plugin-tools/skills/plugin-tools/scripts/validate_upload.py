from runwhen_capability.rwerrors import RwError


def main(ctx, upload_id):
    # NOT RUNNABLE YET: the upload's bytes are only reachable through the runner relay
    # (GET plugin-uploads/{uploadId}), and a task script has no relay. The validate/build code
    # is plugin_tools.upload.run_validate, which takes the bytes directly.
    raise RwError(
        "dependency",
        "validate-upload is not runnable yet: a task script has no way to read the uploaded bytes",
        scope=upload_id,
    )
