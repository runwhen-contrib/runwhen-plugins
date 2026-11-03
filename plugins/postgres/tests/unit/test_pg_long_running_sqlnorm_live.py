"""The SQL query-text normalizer against the real local server: the expression runs in the
database, so its behaviour is the server's regex engine's.

Needs the docker harness (tests/docker/up.sh) on 127.0.0.1:55432 and a psql client; skipped when
either is missing. Each case is a SELECT of the expression over a dollar-quoted constant: nothing
is read or changed."""

import os
import shutil
import subprocess

import pytest
from pg_long_running_queries.sqlnorm import QUERY_CHARS, normalized_query_expr
from pg_long_running_queries.sqltext import normalize_query

PSQL = shutil.which("psql") or next(
    (
        p
        for p in ("/opt/homebrew/opt/libpq/bin/psql", "/usr/local/opt/libpq/bin/psql")
        if os.path.exists(p)
    ),
    None,
)
LIBPQ = {  # throwaway test values of tests/docker/common.sh
    "PGHOST": "127.0.0.1",
    "PGPORT": "55432",
    "PGUSER": "rw_monitor",
    "PGPASSWORD": "rw-test-monitor",
    "PGDATABASE": "orders",
    "PGSSLMODE": "disable",
}


def sql_normalize(raw):
    assert "$zz9$" not in raw
    expr = normalized_query_expr(f"$zz9${raw}$zz9$::text")
    done = subprocess.run(
        [PSQL, "-X", "-A", "-t", "-q", "-c", f"SELECT {expr}"],
        env={**os.environ, **LIBPQ, "PGCONNECT_TIMEOUT": "2"},
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.rstrip("\n")


def reachable():
    if not PSQL:
        return False
    done = subprocess.run(
        [PSQL, "-X", "-A", "-t", "-q", "-c", "SELECT 1"],
        env={**os.environ, **LIBPQ, "PGCONNECT_TIMEOUT": "2"},
        capture_output=True,
        text=True,
    )
    return done.returncode == 0


pytestmark = pytest.mark.skipif(not reachable(), reason="the docker harness is not running")

# (raw text, what the statement returns once the Python pass has collapsed whitespace)
EXACT = [
    ("SELECT 1", "SELECT ?"),
    (
        "select * from t where id = 42 and name = 'bob@example.com'",
        "select * from t where id = ? and name = ?",
    ),
    ("select 'it''s a secret'", "select ?"),
    (
        "select 1.5, .5, 1e10, 2.5E-3, 0x1F, 0b101, 0o17, 1_000_000",
        "select ?, ?, ?, ?, ?, ?, ?, ?",
    ),
    ("select -7", "select -?"),
    ("select E'a\\'b', e'c'", "select ?, ?"),
    ("select $1, $2 from t where a = $1", "select $1, $2 from t where a = $1"),
    ("select t1.col2, x::int4, a$b from t_9", "select t1.col2, x::int4, a$b from t_9"),
    ('select "Order 5" from "my ""tab"" 7"', 'select "Order 5" from "my ""tab"" 7"'),
    ("select \"it's\" from t where a = 'zzz'", 'select "it\'s" from t where a = ?'),
    ("insert into t values (1, 'x'), (2, 'y')", "insert into t values (?, ?), (?, ?)"),
    ("select U&'\\0041bc', b'101', x'ff'", "select U&?, b?, x?"),
    ("select date'2020-01-01', 'x' from t e where e.a='s'", "select date?, ? from t e where e.a=?"),
    ("select '-- not a comment', 3 from t", "select ?, ? from t"),
    (
        "select pg_sleep(600), 'rw-fixture' as marker, 42 as n",
        "select pg_sleep(?), ? as marker, ? as n",
    ),
    ("select \"a\"'x' from t", 'select "a"? from t'),
]
# comments come back as `?` from the statement (the Python pass would have dropped them)
COMMENTS = [
    (
        "select a /* token=abc123 */ from t -- user 55\nwhere b = 1",
        "select a ? from t ? where b = ?",
    ),
    ("select '/*', 3 /* real */ from t", "select ?, ? ? from t"),
]


# From the first dollar-quote opener the statement hides everything to the end of the text: an
# opener cannot be told from a `$$` inside a string, comment or identifier before those are lexed.
DOLLAR = [
    ("select $$he said 'hi' 12$$, $tag$ 99 $$ still inside $tag$", "select ?"),
    ("select a, b from t where c = 5 and d = $$x$$", "select a, b from t where c = ? and d = ?"),
    ("DO $$ BEGIN PERFORM 1; END $$", "DO ?"),
    ("select 'a', $é$ x $é$", "select ?, ?"),
    ("select a$b, $1 from t", "select a$b, $1 from t"),  # not openers
]


@pytest.mark.parametrize("raw, expected", EXACT + COMMENTS + DOLLAR)
def test_the_statement_replaces_literals_and_comments(raw, expected):
    assert normalize_query(sql_normalize(raw)) == expected


@pytest.mark.parametrize("raw, expected", EXACT)
def test_the_statement_alone_already_matches_the_python_normalizer(raw, expected):
    # the Python pass only collapses whitespace here: the SQL did the replacing
    assert normalize_query(sql_normalize(raw)) == normalize_query(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "select 'secret-token-abc",
        "select E'secret\\'rest",
        "select $$secret never closed",
        "select $tag$secret $tag",
        "select /* secret never closed",
        'select "secret never closed',
        "select 'secret' || 'also-secret",
        "select 'a\\' , 'secret2'",
        "select * from t where a = 'x' /* c1 /* c2 /* c3 /* c4 secret */ */ */ */ and b = 5",
        "select /* secret */ 1 -- secret\n, 2",
        "select a from t where b = 'secret' and c = 123456789 and d = $$secret$$",
        # the engine used to backtrack from an unclosed E'' string to the closed E'' and read the
        # rest as a new string (found by the fuzzer): a quote that another quote follows never closes
        " 'ZQXsecret1'  E'''\\'secret2",
        "select E'''\\'secret3",
        "select 'a''secret5",
        "select E'a''\\'secret6 and 7",
    ],
)
def test_text_that_is_cut_off_or_hides_a_value_leaks_nothing(raw):
    out = sql_normalize(raw)
    assert "secret" not in out and "123456789" not in out


# A `$$` that is only text (in a string, a comment or a quoted identifier) used to pair with a real
# opener, and what lay between the two was returned: the first three are the review's live probes.
DOLLAR_LEAKS = [
    "SELECT '$$', $$x$$, 'SECRET_A'",
    "SELECT 'cost: $$' AS c, name FROM users WHERE email = 'bob@example.com' AND note = $$free text SECRET_B$$",
    "SELECT * FROM t WHERE a = '$tag$' AND b = $tag$SECRET_C$tag$ AND c = 'SECRET_D'",
    "SELECT $é$ SECRET_E $é$",
    "SELECT $日本$ SECRET_F $日本$",
    "SELECT $😀$ SECRET_G $😀$, 'SECRET_H'",
    "SELECT $ü9$ SECRET_I $ü9$",
    "SELECT 1 /* $$ */ , 'SECRET_J', $$ y $$",
    "DO $$ BEGIN PERFORM 'SECRET_K'; END $$",
    "SELECT 'a' || '$$' , 'SECRET_L' , $$q$$",
    "SELECT \"a$$b\", 'SECRET_M', $$c$$",
]


@pytest.mark.parametrize("raw", DOLLAR_LEAKS)
def test_a_dollar_pair_that_is_only_text_leaks_nothing_in_the_sql_stage(raw):
    # the statement's own output, before the Python pass can save it
    assert "SECRET" not in sql_normalize(raw)


def test_the_statement_cuts_nothing_by_itself_and_keeps_a_null_a_null():
    long_raw = "select " + ", ".join(f"c{i} = 'v{i}'" for i in range(400))
    out = sql_normalize(long_raw)
    assert len(out) > QUERY_CHARS and "'v1'" not in out  # the cut is the statement's left(), later
    expr = normalized_query_expr("NULL::text")
    done = subprocess.run(
        [PSQL, "-X", "-A", "-t", "-q", "-c", f"SELECT {expr} IS NULL"],
        env={**os.environ, **LIBPQ},
        capture_output=True,
        text=True,
    )
    assert done.stdout.strip() == "t"
