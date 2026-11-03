"""The query-text normalizer: literals never survive, structure does."""

import pytest
from pg_long_running_queries.sqltext import MAX_QUERY_BYTES, clip, normalize_query, safe_query_text


@pytest.mark.parametrize(
    "raw, expected",
    [
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
        ("select $$he said 'hi' 12$$, $tag$ 99 $$ still inside $tag$", "select ?, ?"),
        ("select $1, $2 from t where a = $1", "select $1, $2 from t where a = $1"),
        ("select t1.col2, x::int4, a$b from t_9", "select t1.col2, x::int4, a$b from t_9"),
        ('select "Order 5" from "my ""tab"" 7"', 'select "Order 5" from "my ""tab"" 7"'),
        (
            "select a /* token=abc123 */ from t -- user 55\nwhere b = 1",
            "select a from t where b = ?",
        ),
        ("select /* outer /* nested 9 */ still comment */ 1", "select ?"),
        ("select   a,\n\tb\n from t", "select a, b from t"),
        ("insert into t values (1, 'x'), (2, 'y')", "insert into t values (?, ?), (?, ?)"),
        ("select U&'\\0041bc', b'101', x'ff'", "select U&?, b?, x?"),
        ("select 'a' 'b'", "select ? ?"),
    ],
)
def test_literals_become_placeholders_and_the_rest_is_kept(raw, expected):
    assert normalize_query(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "select 'secret-token-abc",  # string cut off by track_activity_query_size
        "select E'secret\\'rest",  # E-string cut off after an escaped quote
        "select $$secret never closed",
        "select $tag$secret $tag",
        "select /* secret never closed",
        'select "secret never closed',
        "select 'secret' || 'also-secret",
    ],
)
def test_text_that_is_cut_inside_a_literal_or_comment_leaks_nothing(raw):
    assert "secret" not in normalize_query(raw)


def test_a_cut_literal_is_replaced_to_the_end():
    assert normalize_query("select a from t where b = 'abc") == "select a from t where b = ?"


def test_a_comment_marker_inside_a_string_is_not_a_comment():
    assert normalize_query("select '-- not a comment', 3 from t") == "select ?, ? from t"
    assert normalize_query("select '/*', 3 /* real */ from t") == "select ?, ? from t"


def test_clip_cuts_on_a_character_boundary_within_the_byte_budget():
    text = "é" * 600  # 2 bytes each
    cut, truncated = clip(text)
    assert truncated is True
    assert len(cut.encode("utf-8")) <= MAX_QUERY_BYTES
    assert cut == "é" * 512
    assert clip("select ?") == ("select ?", False)
    assert clip("x" * MAX_QUERY_BYTES) == ("x" * MAX_QUERY_BYTES, False)
    assert clip("x" * (MAX_QUERY_BYTES + 1))[1] is True


def test_safe_query_text_normalizes_before_it_cuts():
    # 1500 bytes of raw text that normalizes to well under the budget is not truncated
    raw = "select " + ", ".join(f"'{'v' * 20}'" for _ in range(60))
    text, truncated = safe_query_text(raw)
    assert len(raw) > MAX_QUERY_BYTES and truncated is False
    assert "v" not in text and text.startswith("select ?, ?")
    # a long normalized statement is cut
    text, truncated = safe_query_text("select " + ", ".join(f"c{i}" for i in range(500)))
    assert truncated is True and len(text.encode("utf-8")) <= MAX_QUERY_BYTES


def test_no_text_is_no_text():
    assert safe_query_text(None) == (None, False)
    assert safe_query_text("") == ("", False)
