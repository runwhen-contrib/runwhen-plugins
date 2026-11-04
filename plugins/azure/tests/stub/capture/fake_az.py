"""Run the pinned `az` for real, against a fake Azure Resource Manager.

    FAKE_ARM=/path/rules.json /opt/az/bin/python -I fake_az.py aks show -n x -g rg --subscription <id>

The command line is az's own: argument parsing, the SDK or aaz client it uses, the request it builds,
the way it reads the reply and prints the result or the error are all the CLI's code. Only the two
ends are replaced, so nothing reaches a network (the container runs with `--network none` anyway):

  - sign-in: `Profile.get_login_credentials` / `get_raw_token` answer with a token that is not one.
  - HTTP: `requests` adapters answer from the rules in $FAKE_ARM, a JSON list of
        {"method": "GET", "path": "<regex over the URL path>", "query": {"<name>": "<regex>"},
         "status": 200, "headers": {...}, "body": <JSON>}
    The first rule that matches answers. A request that matches no rule, or one to a host other than
    management.azure.com (az's own update check), is a connection error (a request to a rule-less
    ARM path ends the process with exit code 97 and a message, so a capture never records a guess).

The reply a rule serves is the ARM REST body Microsoft documents for the operation (the capture script
cites the reference page); what az makes of it (null fields, key order, the `resourceGroup` it adds)
is what the CLI prints.
"""

import io
import json
import os
import re
import sys
from urllib.parse import parse_qs, unquote, urlsplit

import requests
from requests.adapters import HTTPAdapter

SUB = "4b1d1f7e-6f0a-4c5e-9a43-0c8a2d9e5b71"
TENANT = "c0ffee00-1234-4abc-8def-0123456789ab"
RULES = json.load(open(os.environ["FAKE_ARM"], encoding="utf-8"))
SERVED = []


def answer(request, rule):
    response = requests.Response()
    response.status_code = rule.get("status", 200)
    response.reason = "OK" if response.status_code < 300 else "Error"
    body = rule.get("body")
    content = b"" if body is None else json.dumps(body).encode()
    response._content = content
    response.headers.update(
        {"Content-Type": "application/json; charset=utf-8", **rule.get("headers", {})}
    )
    response.headers["Content-Length"] = str(len(content))
    response.url = request.url
    response.request = request
    response.raw = io.BytesIO(content)
    return response


def send(self, request, **kwargs):
    parts = urlsplit(request.url)
    if parts.hostname != "management.azure.com":
        raise requests.exceptions.ConnectionError(f"offline: {parts.hostname}")
    path, query = unquote(parts.path), parse_qs(parts.query)
    for rule in RULES:
        if rule["method"] != request.method or not re.fullmatch(rule["path"], path, re.I):
            continue
        if all(
            name in query and re.search(pattern, " ".join(query[name]), re.I)
            for name, pattern in rule.get("query", {}).items()
        ):
            SERVED.append({"method": request.method, "path": path, "query": parts.query})
            return answer(request, rule)
    sys.stderr.write(f"fake_az: no rule for {request.method} {path}?{parts.query}\n")
    sys.stderr.flush()
    os._exit(97)


HTTPAdapter.send = send

from azure.cli.core import _profile  # noqa: E402


class Token:
    def get_token(self, *scopes, **kwargs):
        from azure.core.credentials import AccessToken

        return AccessToken("not-a-token", 4102444800)

    def get_token_info(self, *scopes, **kwargs):
        from azure.core.credentials import AccessTokenInfo

        return AccessTokenInfo("not-a-token", 4102444800)


def get_login_credentials(self, *args, **kwargs):
    return Token(), SUB, TENANT


def get_raw_token(self, *args, **kwargs):
    return ("Bearer", "not-a-token", {"expiresOn": "2100-01-01 00:00:00.000000"}), SUB, TENANT


_profile.Profile.get_login_credentials = get_login_credentials
_profile.Profile.get_raw_token = get_raw_token

from azure.cli.core import get_default_cli  # noqa: E402

code = get_default_cli().invoke(sys.argv[1:], out_file=sys.stdout)
if os.environ.get("FAKE_ARM_SERVED"):
    with open(os.environ["FAKE_ARM_SERVED"], "w", encoding="utf-8") as handle:
        json.dump(SERVED, handle)
sys.exit(code)
