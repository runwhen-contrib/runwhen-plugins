import os

from plugin_tools.credentials import CredentialError, credential_from_env
from plugin_tools.gitrun import GitRunner, RunDir
from plugin_tools.gitsource import GitSource
from plugin_tools.result import now_string
from plugin_tools.spool import ArtifactSpool
from plugin_tools.sync import InputInvalid, SyncRequest, run_sync
from runwhen_capability.rwerrors import RwError


def main(ctx, source, known=None, max_versions=None, limits=None):
    try:
        request = SyncRequest.parse(source, known, max_versions, limits)
        credential = credential_from_env(os.environ)
    except (InputInvalid, CredentialError) as exc:
        raise RwError("invalid_input", str(exc)) from None
    # Built archives are written to $RW_OUT/archive1..archive20, declared artifact outputs the
    # host uploads after this script exits.
    spool = ArtifactSpool.from_env()
    # One HOME and one work dir for the whole run, removed when it ends (pods are reused).
    with RunDir(base=ctx.workdir) as run_dir:
        git = GitSource(
            request.source, GitRunner(run_dir, credential=credential, url=request.source.url)
        )
        manifest = run_sync(request, git, spool, listed_at=now_string(ctx.now()))
    return {"manifest": manifest}
