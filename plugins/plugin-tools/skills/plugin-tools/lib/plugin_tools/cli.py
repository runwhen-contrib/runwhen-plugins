"""`python -m plugin_tools.cli snapshot export`: build the release snapshot from this repo's tags.

For CI to bake the release snapshot image (DESIGN 2.9) without a running registry:

    PYTHONPATH=plugins/plugin-tools/skills/plugin-tools/lib \\
      python -m plugin_tools.cli snapshot export --tags all \\
        --release 2026.11.1 --created-at 2026-11-20T00:00:00Z -o snapshot.tar

It reads a LOCAL clone (`--repo`, default `.`; needs the tags, so `fetch-depth: 0` or `git fetch
--tags`), never the network, and builds each tag with the same `prepare` the sync task runs, so its
bytes are the bytes a registry that synced the same tags would export. The runwhen-capability SDK
that compiles is the installed one: pin it in CI. Prints `sha256:<hex>  <file>`; record that as the
snapshot hash in the platform release manifest.

    --tags       `all` (every plugin tag matching --tag-pattern, and every runtimes/<name>/v<ver> tag)
                 or a comma-separated list of tag names (plugin tags and/or runtime tags)
    --repo-url   what each entry records as `source.repo` (default the public repository's URL;
                 never the local path)

Exit codes: 0 done; 1 a selected tag was rejected (listed on stderr, no output file); 2 usage error
or a git failure.
"""

import argparse
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from plugin_tools.archive import ArchiveError
from plugin_tools.gitrun import GitError, GitRunner, RunDir
from plugin_tools.gitsource import GitSource, RuntimeInvalid, SourceInvalid, SourceSpec, TagRef
from plugin_tools.prepare import Rejected, prepare, reject_code
from plugin_tools.semver import parse_semver
from plugin_tools.snapshot import SnapshotVersion, export_snapshot

EXIT_OK = 0
EXIT_REJECTED = 1
EXIT_ERROR = 2
DEFAULT_SOURCE_ID = "runwhen-plugins"
DEFAULT_REPO_URL = "https://github.com/runwhen-contrib/runwhen-plugins"


class CliError(Exception):
    """A failure to report on stderr; `code` is the process exit code."""

    def __init__(self, message: str, code: int = EXIT_ERROR) -> None:
        super().__init__(message)
        self.code = code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="plugin_tools.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("snapshot", help="air-gap snapshots").add_subparsers(
        dest="snapshot_command", required=True
    )
    export = snapshot.add_parser(
        "export",
        help="build a snapshot from the tags of a local plugins repository",
        description=(
            "Build the air-gap snapshot (a tar) from a local clone's tags, with the same "
            "validation as the sync task. Prints 'sha256:<hex>  <file>' on success."
        ),
    )
    export.add_argument(
        "--tags", required=True, help="'all' or a comma-separated list of tag names"
    )
    export.add_argument("--repo", default=".", help="local clone (default %(default)s)")
    export.add_argument(
        "--repo-url", default=DEFAULT_REPO_URL, help="source.repo each entry records"
    )
    export.add_argument("--tag-pattern", default="{name}/v{version}", help="default %(default)s")
    export.add_argument("--path", default="plugins/{name}", help="default %(default)s")
    export.add_argument("--source-id", default=DEFAULT_SOURCE_ID, help="default %(default)s")
    export.add_argument("--release", help="release label recorded in the snapshot manifest")
    export.add_argument(
        "--created-at",
        metavar="ISO8601",
        help="pin createdAt and every ingestedAt (e.g. 2026-11-05T12:00:00Z) for reproducible "
        "builds; default is the current UTC time",
    )
    export.add_argument("-o", "--output", required=True, help="snapshot file to write")
    return parser


def _parse_created_at(value: str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise CliError(f"--created-at {value!r} is not an ISO-8601 timestamp") from None
    # A timestamp without an offset is read as UTC, so the same text always gives the same bytes.
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _git_dir(repo: str) -> Path:
    path = Path(repo)
    for candidate in (path / ".git", path):  # a work tree, or a bare repo
        if (candidate / "objects").is_dir() and (candidate / "HEAD").exists():
            return candidate.resolve()
    raise CliError(f"{repo} is not a git repository")


def list_local_tags(git: GitSource) -> dict[str, str]:
    """Tag name -> commit sha for every tag of the local repo (annotated tags peeled)."""
    out = git.git(
        "for-each-ref", "--format=%(refname:strip=2)%09%(objectname)%09%(*objectname)", "refs/tags"
    ).decode("utf-8", "replace")
    tags = {}
    for line in out.splitlines():
        name, _, rest = line.partition("\t")
        direct, _, peeled = rest.partition("\t")
        tags[name] = peeled or direct
    return tags


def select_tags(git: GitSource, selection: str) -> tuple[list[TagRef], list[TagRef]]:
    """(plugin tags, runtime tags) the `--tags` value names."""
    local = list_local_tags(git)
    if selection.strip() == "all":
        chosen = local
    else:
        wanted = [t.strip() for t in selection.split(",") if t.strip()]
        if not wanted:
            raise CliError("--tags is empty")
        missing = [t for t in wanted if t not in local]
        if missing:
            raise CliError(f"no such tag: {', '.join(missing)}")
        chosen = {t: local[t] for t in wanted}
    plugin_tags = git.matching_tags(chosen)
    runtime_tags = git.matching_runtime_tags(chosen)
    covered = {t.tag for t in plugin_tags} | {t.tag for t in runtime_tags}
    stray = [t for t in chosen if t not in covered]
    if stray and selection.strip() != "all":
        raise CliError(
            f"tag does not match the tag pattern or runtimes/<name>/v<version>: {', '.join(stray)}"
        )
    return plugin_tags, runtime_tags


def export(args: argparse.Namespace) -> str:
    """Build the snapshot at `args.output`; returns its bare sha256 hex."""
    now = _parse_created_at(args.created_at)
    out = Path(args.output)
    if not out.parent.is_dir():
        raise CliError(f"output directory {out.parent} does not exist")
    try:
        spec = SourceSpec.parse(
            {
                "id": args.source_id,
                "url": args.repo_url,
                "tagPattern": args.tag_pattern,
                "path": args.path,
            }
        )
    except SourceInvalid as exc:
        raise CliError(f"invalid source: {exc}") from None
    gitdir = _git_dir(args.repo)
    with RunDir() as run_dir:
        git = GitSource(spec, GitRunner(run_dir, gitdir=gitdir, url=spec.url))
        try:
            plugin_tags, runtime_tags = select_tags(git, args.tags)
            versions, runtimes, rejected = _build(git, spec, plugin_tags, runtime_tags)
        except GitError as exc:
            raise CliError(f"git failed: {exc}") from None
        if rejected:
            raise CliError(
                f"{len(rejected)} tag(s) were rejected; no snapshot written:\n"
                + "\n".join(f"  {tag}: {code}: {detail}" for tag, code, detail in rejected),
                EXIT_REJECTED,
            )
        # Beside the target so the final rename never crosses a filesystem.
        with tempfile.NamedTemporaryFile(
            dir=out.parent, prefix=f".{out.name}.", suffix=".part", delete=False
        ) as part:
            part_path = Path(part.name)
            try:
                sha = export_snapshot(part, versions, runtimes, release=args.release, now=now)
            except BaseException:
                part_path.unlink(missing_ok=True)
                raise
        try:
            os.replace(part_path, out)
        except BaseException:
            part_path.unlink(missing_ok=True)
            raise
    return sha


def _build(git: GitSource, spec: SourceSpec, plugin_tags, runtime_tags):
    versions: list[SnapshotVersion] = []
    runtimes = []
    rejected: list[tuple[str, str, str]] = []
    for tag in runtime_tags:
        try:
            parse_semver(tag.version)
            runtimes.append(git.read_runtime(tag))
        except (ValueError, RuntimeInvalid) as exc:
            rejected.append((tag.tag, "E_PLUGIN_INVALID", getattr(exc, "detail", str(exc))))
        except ArchiveError as exc:
            rejected.append((tag.tag, reject_code(exc.code), exc.detail))
    for tag in plugin_tags:
        path = spec.path.replace("{name}", tag.name)
        try:
            parse_semver(tag.version)
            if git.object_type(tag.commit) != "commit":
                raise ArchiveError("E_INVALID_PLUGIN", "tag does not point at a commit")
            files = git.read_tree(tag.commit, path)
        except ValueError as exc:
            rejected.append((tag.tag, "E_PLUGIN_INVALID", str(exc)))
            continue
        except ArchiveError as exc:
            rejected.append((tag.tag, reject_code(exc.code), exc.detail))
            continue
        result = prepare(files, expected_name=tag.name, expected_version=tag.version)
        if isinstance(result, Rejected):
            rejected.append((tag.tag, result.code, result.detail))
            continue
        source = {
            "kind": "public",
            "id": spec.id,
            "repo": spec.url,
            "path": path,
            "tag": tag.tag,
            "commit": tag.commit,
        }
        versions.append(SnapshotVersion(result, source))
    return versions, runtimes, rejected


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        sha = export(args)
    except CliError as exc:
        print(f"plugin_tools.cli: {exc}", file=sys.stderr)
        return exc.code
    except OSError as exc:
        print(f"plugin_tools.cli: {exc.strerror or type(exc).__name__}", file=sys.stderr)
        return EXIT_ERROR
    print(f"sha256:{sha}  {args.output}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
