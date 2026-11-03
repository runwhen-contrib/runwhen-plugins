"""The integration's password auth method: its script (run as the executor runs it, with
temp dirs), its fit with the auth-plan contract, and the recognizer."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from runwhen_capability.contracts import validate

PLUGIN = Path(__file__).parents[2]
INTEGRATION = PLUGIN / "integration"
SCRIPT = INTEGRATION / "auth" / "password.py"
SECRET = "p@ss w0rd/with'odd\\chars"

ENV_NAMES = ["PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE", "PGSSLMODE"]


def load():
    spec = importlib.util.spec_from_file_location("pg_auth_password", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_script(tmp_path, ref=None, inputs=None, *, as_process=False):
    """(exit code, parsed $RW_AUTH_OUT_ENV or None, error JSON or None, stdout+stderr)."""
    ref_dir = tmp_path / "ref"
    ref_dir.mkdir(exist_ok=True)
    for key, value in (ref if ref is not None else {"password": SECRET}).items():
        (ref_dir / key).write_text(value, encoding="utf-8")
    out_env, out_error = tmp_path / "out.env", tmp_path / "out.error"
    env = {
        "RW_AUTH_REF_DIR": str(ref_dir),
        "RW_AUTH_OUT_ENV": str(out_env),
        "RW_AUTH_OUT_ERROR": str(out_error),
    }
    given = {"HOST": "db.shop.svc", "USER": "app"} if inputs is None else inputs
    env.update({f"RW_AUTH_INPUT_{k}": v for k, v in given.items()})
    if as_process:
        done = subprocess.run(
            [sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, cwd=INTEGRATION
        )
        code, shown = done.returncode, done.stdout + done.stderr
    else:
        code, shown = load().main(env), ""
    parsed = None
    if out_env.exists():
        parsed = dict(line.split("=", 1) for line in out_env.read_text().splitlines())
    error = json.loads(out_error.read_text()) if out_error.exists() else None
    return code, parsed, error, shown


def test_the_env_is_written_in_the_credential_types_names(tmp_path):
    code, env, error, _ = run_script(tmp_path)
    assert (code, error) == (0, None)
    assert list(env) == ENV_NAMES
    assert env == {
        "PGHOST": "db.shop.svc",
        "PGPORT": "5432",
        "PGUSER": "app",
        "PGPASSWORD": SECRET,
        "PGDATABASE": "postgres",
        "PGSSLMODE": "require",
    }


def test_the_inputs_override_the_defaults(tmp_path):
    inputs = {
        "HOST": "h",
        "USER": "u",
        "PORT": "6432",
        "DATABASE": "orders",
        "SSLMODE": "verify-full",
    }
    _, env, _, _ = run_script(tmp_path, inputs=inputs)
    assert (env["PGPORT"], env["PGDATABASE"], env["PGSSLMODE"]) == ("6432", "orders", "verify-full")


@pytest.mark.parametrize("key", ["password", "PGPASSWORD", "value"])
def test_the_password_is_read_from_each_accepted_key(tmp_path, key):
    code, env, _, _ = run_script(tmp_path, ref={key: SECRET})
    assert code == 0 and env["PGPASSWORD"] == SECRET


@pytest.mark.parametrize("ending", ["\n", "\r\n"])
def test_one_trailing_newline_is_not_part_of_the_password(tmp_path, ending):
    _, env, _, _ = run_script(tmp_path, ref={"password": "abc" + ending})
    assert env["PGPASSWORD"] == "abc"


@pytest.mark.parametrize(
    "ref,why",
    [
        ({}, "none of them"),
        ({"password": "a", "PGPASSWORD": "b"}, "password, PGPASSWORD"),
        ({"password": "a", "value": "b", "PGPASSWORD": "c"}, "exactly one"),
        ({"token": "a"}, "none of them"),
        ({"password": ""}, "empty"),
        ({"password": "\n"}, "empty"),
        ({"password": "line1\nline2"}, "single line"),
        ({"password": "a\x00b"}, "single line"),
        ({"password": "a\n\n"}, "single line"),
    ],
)
def test_a_reference_without_exactly_one_usable_password_is_invalid_input(tmp_path, ref, why):
    code, env, error, _ = run_script(tmp_path, ref=ref)
    assert code == 1 and env is None
    assert error["code"] == "invalid_input" and why in error["message"]
    assert set(error) == {"code", "message"}


@pytest.mark.parametrize(
    "inputs",
    [
        {"USER": "app"},  # no host
        {"HOST": "h"},  # no user
        {"HOST": "h\nPGSSLMODE=disable", "USER": "u"},  # a newline would add a variable
        {"HOST": "h", "USER": "u\r"},
        {"HOST": "h", "USER": "u", "DATABASE": "a\nb"},
        {"HOST": "h", "USER": "u", "PORT": "0"},
        {"HOST": "h", "USER": "u", "PORT": "65536"},
        {"HOST": "h", "USER": "u", "PORT": "54x2"},
        {"HOST": "h", "USER": "u", "PORT": "-1"},
        {"HOST": "h", "USER": "u", "SSLMODE": "none"},
    ],
)
def test_a_bad_input_is_invalid_input_and_writes_no_env(tmp_path, inputs):
    code, env, error, _ = run_script(tmp_path, inputs=inputs)
    assert code == 1 and env is None
    assert error["code"] == "invalid_input"


def test_a_missing_reference_directory_is_invalid_input(tmp_path):
    mod = load()
    env = {"RW_AUTH_OUT_ENV": str(tmp_path / "o"), "RW_AUTH_OUT_ERROR": str(tmp_path / "e"),
           "RW_AUTH_INPUT_HOST": "h", "RW_AUTH_INPUT_USER": "u"}  # fmt: skip
    assert mod.main(env) == 1
    assert json.loads((tmp_path / "e").read_text())["code"] == "invalid_input"


def test_the_password_is_never_printed_or_put_in_an_error(tmp_path):
    for ref, inputs in [
        (None, None),  # success
        ({"password": SECRET, "value": SECRET}, None),  # two keys
        (None, {"HOST": "h", "USER": "u", "PORT": SECRET}),
        ({"password": SECRET + "\nmore"}, None),
    ]:
        sub = tmp_path / str(abs(hash(str(ref) + str(inputs))))
        sub.mkdir()
        code, _, error, shown = run_script(sub, ref, inputs, as_process=True)
        assert SECRET not in shown and SECRET not in json.dumps(error)


def test_it_runs_as_a_plain_script_with_python3(tmp_path):
    code, env, _, shown = run_script(tmp_path, as_process=True)
    assert code == 0 and env["PGHOST"] == "db.shop.svc"
    assert SECRET not in shown and shown == ""


def test_a_failure_exits_non_zero_so_the_task_run_fails(tmp_path):
    code, _, error, _ = run_script(tmp_path, ref={}, as_process=True)
    assert code == 1 and error["code"] == "invalid_input"


# ---- the method and the credential type, against the contracts ------------------------------


def integration():
    doc = yaml.safe_load((INTEGRATION / "integration.yaml").read_text())
    return doc


def test_the_method_is_a_script_method_with_the_agreed_shape():
    method = integration()["authMethods"]["password"]
    assert method["script"] == "auth/password.py"
    assert "native" not in method
    assert method["produces"] == "postgres/libpq"
    assert method["accepts"] == ["k8s-secret", "vault", "file"]
    assert (INTEGRATION / method["script"]).is_file()
    inputs = method["inputs"]
    assert set(inputs) == {"host", "port", "user", "database", "sslmode"}
    assert inputs["port"]["default"] == 5432 and inputs["database"]["default"] == "postgres"
    assert inputs["sslmode"]["default"] == "require"
    assert inputs["sslmode"]["enum"] == [
        "disable", "allow", "prefer", "require", "verify-ca", "verify-full",
    ]  # fmt: skip
    # the script and the method agree on the defaults and the enum
    mod = load()
    assert mod.SSLMODES == tuple(inputs["sslmode"]["enum"])
    assert (mod.DEFAULT_SSLMODE, mod.DEFAULT_DATABASE) == ("require", "postgres")
    assert int(mod.DEFAULT_PORT) == 5432


def test_the_credential_type_is_env_only_and_the_script_writes_exactly_those_names(tmp_path):
    ctype = integration()["credentialTypes"]["postgres/libpq"]
    assert ctype["materialize"] == {"env": ENV_NAMES}  # no file target, so no PGSSLROOTCERT
    assert "content" not in ctype  # a script method matches references by kind
    _, env, _, _ = run_script(tmp_path)
    assert list(env) == ctype["materialize"]["env"]


def test_a_compiled_auth_plan_for_the_method_fits_the_auth_plan_schema(tmp_path):
    doc = integration()
    method = doc["authMethods"]["password"]
    plan = {
        "connection": "orders-db",
        "integration": "postgres@0.1.0",
        "authMethod": "password",
        "credentialType": method["produces"],
        "reference": {"kind": "k8s-secret", "ref": "shop/orders-db-credentials"},
        "method": {
            "script": {
                "path": f"integration/{method['script']}",
                "archive": {"sha256": "sha256:" + "0" * 64, "size": 4096},
            }
        },
        "inputs": {
            "host": "orders-db.shop.svc",
            "port": 5432,
            "user": "app",
            "database": "postgres",
            "sslmode": "require",
        },  # fmt: skip
        "materialize": doc["credentialTypes"][method["produces"]]["materialize"],
    }
    assert validate("auth-plan", plan) == []
    # the old shape (env and a file target in one plan) does not fit: one target per plan
    both = {
        **plan,
        "materialize": {**plan["materialize"], "file": {"env": "PGSSLROOTCERT", "mode": "0600"}},
    }
    assert validate("auth-plan", both) != []
    # and the plan is what the script is fed: its inputs are the method's declared inputs
    assert set(plan["inputs"]) == set(method["inputs"])


# ---- the recognizer --------------------------------------------------------------------------


def recognizer():
    (rec,) = integration()["recognizers"]
    return rec


def test_the_recognizer_needs_port_5432_and_a_non_headless_service_in_every_matcher():
    rec = recognizer()
    assert rec["from"] == "kubernetes/service"
    for matcher in rec["when"]:
        doc = matcher.get("doc", {})
        assert 5432 in doc.values(), "a matcher without the port test would stand on labels alone"
        assert doc.get("spec.clusterIP != 'None'") is True


def test_labels_never_stand_alone_in_the_recognizer():
    for matcher in recognizer()["when"]:
        assert "doc" in matcher


def test_the_recognizer_proposes_port_5432_and_the_password_method():
    propose = recognizer()["propose"]
    assert propose["identity"]["port"] == 5432 and propose["inputs"]["port"] == 5432
    assert propose["authMethod"] == "password"
    assert propose["name"] == "{{namespace}}-{{name}}"  # the templates have no cluster variable


def test_the_recognizer_documents_why_pooler_proposals_are_safe():
    text = (INTEGRATION / "integration.yaml").read_text()
    assert "pgbouncer" in text and "read-only" in text
