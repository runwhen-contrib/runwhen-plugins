"""The `GIT_ASKPASS` program: answers git's credential prompts from the environment.

git runs it as `python3 -I askpass.py "Username for 'https://host': "` (through a shell, so
`GIT_ASKPASS` may be a command line) and reads one line from stdout. It never writes the secret
anywhere else and prints nothing about it on failure. Self-contained on purpose: it runs as a
script under `-I`, so it imports nothing from this package.
"""

import os
import sys

ENV_USERNAME = "PLUGIN_TOOLS_GIT_USERNAME"
ENV_PASSWORD = "PLUGIN_TOOLS_GIT_PASSWORD"


def answer(prompt, environ):
    # git's prompts are "Username for '<url>': " and "Password for '<url>': "; the URL may
    # itself say "username", so only the start decides.
    name = ENV_USERNAME if prompt.lstrip().lower().startswith("username") else ENV_PASSWORD
    return environ.get(name)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    value = answer(args[0] if args else "", os.environ)
    if value is None:
        sys.stderr.write("askpass: no credential in the environment\n")
        return 1
    sys.stdout.write(value + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
