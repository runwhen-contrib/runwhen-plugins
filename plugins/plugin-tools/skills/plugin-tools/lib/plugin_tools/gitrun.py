"""The one place a git process is started (S-D4, DESIGN 2.3 "Hardening, on every git call").

Every git call of a run goes through `GitRunner`, which applies, to each of them:

- config flags (`HARDENING_FLAGS`): `-c core.hooksPath=/dev/null -c protocol.allow=never
  -c protocol.https.allow=always`, so no client hook runs and https is the only transport;
- the environment: `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`, `GIT_TERMINAL_PROMPT=0`,
  a per-run `HOME` and `TMPDIR`, and nothing else from the task's environment except an allow-list
  (`PASS_ENV`: PATH, locale, proxy and CA settings). The task's own `GIT_TOKEN` is not on it;
- the credential, for the calls that contact the remote only (`remote=True`), through
  `GIT_ASKPASS`, with `-c http.followRedirects=false` so a redirect cannot carry it to another
  host;
- a timeout, and scrubbing of every secret from what git said before it is raised or logged.

A run owns one `HOME` and one work directory; `RunDir` creates them and removes them at the end
(pods are reused under D118, so nothing may outlive the run).

Test-only: `file://` is blocked by `protocol.allow=never`. A test that needs a local repository
passes `test_allow_protocols=("file",)` to `GitRunner`, which adds `-c protocol.<name>.allow=always`
for each; it refuses unless `PLUGIN_TOOLS_ALLOW_TEST_PROTOCOLS=1` is in the process environment
(the unit tests' conftest sets it). No task input reaches this argument, and the executor never sets
that variable.
"""

import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from plugin_tools.credentials import GitCredential, auth_env

GIT_TIMEOUT = 300

HARDENING_FLAGS: tuple[str, ...] = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "protocol.allow=never",
    "-c",
    "protocol.https.allow=always",
)
# Only for calls that contact the remote: git's default follows a redirect from the first request
# and sends the Authorization header to wherever it points. A moved repository must be
# reconfigured with its new URL.
REMOTE_FLAGS: tuple[str, ...] = ("-c", "http.followRedirects=false")
FIXED_ENV: dict[str, str] = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
}
# What a git process may inherit from the task's environment: where to find git, how to talk to
# the network (proxy, CA bundle), nothing that holds a credential.
PASS_ENV: tuple[str, ...] = (
    "PATH",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "GIT_SSL_CAINFO",
    "GIT_SSL_CAPATH",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
)
TEST_PROTOCOLS_ENV = "PLUGIN_TOOLS_ALLOW_TEST_PROTOCOLS"

# A secret this short is only replaced as a whole word, so scrubbing cannot mangle diagnostics.
_BARE_SECRET_MIN = 8


class GitError(Exception):
    """A git call failed. The message never contains a credential or the URL's userinfo."""

    # What the failed command printed on stdout, scrubbed (`git push --porcelain` reports each
    # rejected ref there). Empty when the command never ran or timed out.
    stdout: str = ""


class GitTimeout(GitError):
    """A git call ran past its timeout."""


def strip_userinfo(url: str) -> str:
    """Drop `user:password@` from a scheme URL (scp-style `git@host:path` is left alone)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if "@" not in parts.netloc:
        return url
    return urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[1]))


def _subcommand(cmd: Sequence[str]) -> str:
    """The git subcommand of `git [-c k=v | --option]... <subcommand> ...`, for messages."""
    args = iter(cmd[1:])
    for arg in args:
        if arg == "-c":
            next(args, None)  # its value
        elif not arg.startswith("-"):
            return arg
    return cmd[-1]


class RunDir:
    """A run's private `HOME` and work directory, removed by `close` (use it as a context)."""

    def __init__(self, base: Path | str | None = None) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="plugin-tools-", dir=base))
        self.home = self.root / "home"
        self.work = self.root / "work"
        self.tmp = self.root / "tmp"
        for d in (self.home, self.work, self.tmp):
            d.mkdir(mode=0o700)

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self) -> "RunDir":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class GitRunner:
    def __init__(
        self,
        run_dir: RunDir,
        *,
        gitdir: Path | None = None,
        credential: GitCredential | None = None,
        url: str | None = None,
        timeout: float = GIT_TIMEOUT,
        test_allow_protocols: Sequence[str] = (),
        environ: Mapping[str, str] | None = None,
    ) -> None:
        if test_allow_protocols and os.environ.get(TEST_PROTOCOLS_ENV) != "1":
            raise RuntimeError("test_allow_protocols is for tests only")
        self.run_dir = run_dir
        self.workdir = run_dir.work
        self.gitdir = gitdir or run_dir.work / "repo.git"
        self.credential = credential
        self.url = url
        self.timeout = timeout
        self._test_protocols = tuple(test_allow_protocols)
        self._environ = os.environ if environ is None else environ

    # -- the one definition of "a hardened git call" -----------------------------------------

    def flags(self, *, remote: bool = False) -> list[str]:
        flags = list(HARDENING_FLAGS)
        for name in self._test_protocols:
            flags += ["-c", f"protocol.{name}.allow=always"]
        if remote:
            flags += REMOTE_FLAGS
        return flags

    def command(self, *args: str, remote: bool = False) -> list[str]:
        return ["git", *self.flags(remote=remote), *args]

    def env(
        self, *, remote: bool = False, extra: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        env = {k: self._environ[k] for k in PASS_ENV if k in self._environ}
        env.update(FIXED_ENV)
        env["HOME"] = str(self.run_dir.home)
        env["TMPDIR"] = str(self.run_dir.tmp)
        env["GIT_DIR"] = str(self.gitdir)
        if remote:
            env.update(auth_env(self.credential))
        env.update(extra or {})
        return env

    # -- running -----------------------------------------------------------------------------

    def scrub(self, text: str) -> str:
        """Remove credentials from anything git said before it is raised or logged.

        Only exact secret values are replaced: the URL's userinfo (as `user:pw@`, or the whole
        userinfo when it is a bare token), its password, and the credential's token. A secret
        shorter than `_BARE_SECRET_MIN` is replaced only as a whole word.
        """
        secrets: list[str] = []
        url = self.url or ""
        safe = strip_userinfo(url)
        if url and safe != url:
            text = text.replace(url, safe)
            userinfo = urlsplit(url).netloc.rsplit("@", 1)[0]
            text = text.replace(userinfo + "@", "")
            user, colon, password = userinfo.partition(":")
            secrets.append(password if colon else user)
        if self.credential is not None:
            secrets.append(self.credential.password)
        for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
            if len(secret) >= _BARE_SECRET_MIN:
                text = text.replace(secret, "***")
            else:
                word = rf"(?<![0-9A-Za-z]){re.escape(secret)}(?![0-9A-Za-z])"
                text = re.sub(word, "***", text)
        return text

    def ensure_repo(self) -> None:
        """Create the bare repo of this run (`init` writes HEAD through a ref transaction, so
        it is hardened like any other call)."""
        self.workdir.mkdir(parents=True, exist_ok=True)
        if not self.gitdir.exists():
            self._run(self.command("init", "--bare", "-q", str(self.gitdir)))

    def git(
        self,
        *args: str,
        remote: bool = False,
        env: Mapping[str, str] | None = None,
        stdin: bytes | None = None,
    ) -> bytes:
        """Run git against this run's bare repo; returns stdout."""
        self.ensure_repo()
        return self._run(
            self.command(*args, remote=remote), self.env(remote=remote, extra=env), stdin
        )

    def _run(
        self,
        cmd: list[str],
        env: Mapping[str, str] | None = None,
        stdin: bytes | None = None,
    ) -> bytes:
        try:
            proc = subprocess.run(
                cmd,
                cwd=self.workdir,
                env=dict(env) if env is not None else self.env(),
                input=stdin,
                check=True,
                capture_output=True,
                timeout=self.timeout,
            )
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode("utf-8", "replace").strip()
            # Scrub all of stderr, then cut: a secret that straddles the cut must not leave a
            # fragment the exact-match scrub cannot see.
            tail = self.scrub(stderr)[-500:]
            error = GitError(f"git {_subcommand(cmd)} failed: {tail}")
            error.stdout = self.scrub((exc.stdout or b"").decode("utf-8", "replace"))
            raise error from None
        except subprocess.TimeoutExpired:
            raise GitTimeout(f"git {_subcommand(cmd)} timed out after {self.timeout}s") from None
        except OSError as exc:
            raise GitError(f"cannot run git: {exc.strerror or type(exc).__name__}") from None
        return proc.stdout

    def popen_batch(self) -> tuple[subprocess.Popen, threading.Timer, threading.Event]:
        """A hardened `git cat-file --batch` process, with its kill timer and expiry flag."""
        self.ensure_repo()
        try:
            proc = subprocess.Popen(
                self.command("cat-file", "--batch"),
                cwd=self.workdir,
                env=self.env(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise GitError(f"cannot run git: {exc.strerror or type(exc).__name__}") from None
        expired = threading.Event()

        def expire() -> None:
            expired.set()
            proc.kill()

        timer = threading.Timer(self.timeout, expire)
        timer.start()
        return proc, timer, expired
