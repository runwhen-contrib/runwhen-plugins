"""The git credential of a sync run, and how it reaches git.

The connection's credential type (`git/https-token` or `git/github-app`, see
integration/integration.yaml) materializes two environment variables for the task:

    GIT_TOKEN      the token (a personal or deploy token, or a GitHub App installation token)
    GIT_USERNAME   optional; `x-access-token` when absent, the form GitHub-style hosts accept

This module reads them. They are presented to git only through `GIT_ASKPASS`: `auth_env` is the
environment of the git calls that talk to the remote (and no other call gets it), and the
`GitRunner` builds that environment from an allow-list, so the task's own `GIT_TOKEN` is never
inherited by a git process that does not need it. The values are never in argv, a URL or a log.
"""

import shlex
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from plugin_tools.askpass import ENV_PASSWORD, ENV_USERNAME

ENV_TOKEN = "GIT_TOKEN"
ENV_TOKEN_USERNAME = "GIT_USERNAME"
DEFAULT_USERNAME = "x-access-token"

ASKPASS_PATH = Path(__file__).with_name("askpass.py")


class CredentialError(Exception):
    """The credential in the environment is unusable. The message never holds a value."""


@dataclass(frozen=True, repr=False)
class GitCredential:
    username: str
    password: str

    def __repr__(self) -> str:
        return f"GitCredential(username={self.username!r}, password='***')"


def credential_from_env(environ: Mapping[str, str]) -> GitCredential | None:
    """The credential the connection materialized; None when there is none (anonymous access)."""
    token = environ.get(ENV_TOKEN)
    if token is None or token == "":
        return None
    username = environ.get(ENV_TOKEN_USERNAME) or DEFAULT_USERNAME
    for name, value in ((ENV_TOKEN, token), (ENV_TOKEN_USERNAME, username)):
        if "\0" in value or "\n" in value or "\r" in value:
            raise CredentialError(f"{name} must be a single line without NUL")
    return GitCredential(username, token)


def askpass_command() -> str:
    """The `GIT_ASKPASS` value: this interpreter, isolated, running askpass.py (git uses a shell)."""
    return f"{shlex.quote(sys.executable)} -I {shlex.quote(str(ASKPASS_PATH))}"


def auth_env(cred: GitCredential | None) -> dict[str, str]:
    """The environment that lets git answer its username and password prompts; {} without one."""
    if cred is None:
        return {}
    return {
        "GIT_ASKPASS": askpass_command(),
        ENV_USERNAME: cred.username,
        ENV_PASSWORD: cred.password,
    }
