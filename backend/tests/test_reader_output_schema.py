"""
tests/test_reader_output_schema.py — ReaderOutput.from_raw structured-output
validation gate.

Stage-1 Atelier: ``ReaderOutput.from_raw`` now routes through a Pydantic v2
schema (``models._ReaderOutputSchema``) after a code-fence / JSON-envelope
cleanup pass. The cleanup tolerance from the old hand-rolled parser MUST
survive the refactor — the Reader is an LLM, and its output regularly
includes prose, fences, trailing commas (no — actually json.loads can't
parse those, but everything else is fair game), and partially-typed lists.

These tests pin both halves:
  * Cleanup tolerance (fences, surrounding prose, missing braces)
  * Pydantic schema validation (per-field coercion, ignored unknowns,
    empty defaults on missing fields, drop-not-fail on bad item types)
"""

from __future__ import annotations

from models import ReaderOutput


# ── Happy path ───────────────────────────────────────────────────────────────

def test_full_well_formed_payload():
    raw = (
        '{"intent": "draft a tweet", '
        '"constraints": ["under 280 chars", "no hashtags"], '
        '"relevant_facts": ["account is professional"], '
        '"proposed_tools": ["search"], '
        '"red_flags": []}'
    )
    out = ReaderOutput.from_raw(raw)
    assert out.intent == "draft a tweet"
    assert out.constraints == ("under 280 chars", "no hashtags")
    assert out.relevant_facts == ("account is professional",)
    assert out.proposed_tools == ("search",)
    assert out.red_flags == ()


def test_minimal_payload_defaults_to_empty_tuples():
    out = ReaderOutput.from_raw('{"intent": "just say hi"}')
    assert out.intent == "just say hi"
    assert out.constraints == ()
    assert out.relevant_facts == ()
    assert out.proposed_tools == ()
    assert out.red_flags == ()


# ── Cleanup tolerance: the parts NOT delegated to Pydantic ──────────────────

def test_code_fence_is_stripped():
    raw = (
        "```json\n"
        '{"intent": "fenced", "proposed_tools": ["a", "b"]}\n'
        "```"
    )
    out = ReaderOutput.from_raw(raw)
    assert out.intent == "fenced"
    assert out.proposed_tools == ("a", "b")


def test_plain_backtick_fence_is_stripped():
    raw = (
        "```\n"
        '{"intent": "plain", "constraints": ["c1"]}\n'
        "```"
    )
    out = ReaderOutput.from_raw(raw)
    assert out.intent == "plain"
    assert out.constraints == ("c1",)


def test_prose_before_and_after_json_is_ignored():
    raw = (
        "Here is my analysis of the request:\n"
        '{"intent": "compute", "relevant_facts": ["x is 7"]}\n'
        "End of plan."
    )
    out = ReaderOutput.from_raw(raw)
    assert out.intent == "compute"
    assert out.relevant_facts == ("x is 7",)


# ── Pydantic schema: coercion + tolerance ───────────────────────────────────

def test_mixed_type_list_drops_unsupported_entries():
    """The Reader occasionally emits objects inside string lists (e.g.
    `{"k": "v"}`). The validator must drop those without failing the turn."""
    raw = (
        '{"intent": "ok", '
        '"constraints": ["a", 2, 3.14, {"k": "v"}, ["nested"], null]}'
    )
    out = ReaderOutput.from_raw(raw)
    # str/int/float survive; dict, list, null are dropped.
    assert out.constraints == ("a", "2", "3.14")


def test_unknown_fields_are_ignored():
    raw = (
        '{"intent": "ok", "proposed_tools": ["x"], '
        '"extra_field": "ignored", "another_extra": 42}'
    )
    out = ReaderOutput.from_raw(raw)
    assert out.intent == "ok"
    assert out.proposed_tools == ("x",)


def test_null_list_fields_become_empty_tuples():
    raw = '{"intent": "ok", "constraints": null, "proposed_tools": null}'
    out = ReaderOutput.from_raw(raw)
    assert out.constraints == ()
    assert out.proposed_tools == ()


def test_non_list_for_list_field_becomes_empty():
    raw = '{"intent": "ok", "proposed_tools": "search,calc"}'
    out = ReaderOutput.from_raw(raw)
    # We don't comma-split a string masquerading as a list — drop to empty
    # so the Actor doesn't act on accidentally-stringified plans.
    assert out.proposed_tools == ()


# ── Failure modes that produce a degraded but well-formed result ────────────

def test_empty_input_returns_empty_reader_output():
    out = ReaderOutput.from_raw("")
    assert out.intent == ""
    assert out.proposed_tools == ()


def test_garbage_input_returns_empty_reader_output():
    out = ReaderOutput.from_raw("the reader was very confused today")
    assert out.intent == ""


def test_unterminated_brace_returns_empty_reader_output():
    out = ReaderOutput.from_raw('{"intent": "x"')
    assert out.intent == ""


def test_non_object_json_returns_empty_reader_output():
    """A JSON array (not an object) shouldn't crash the validator."""
    out = ReaderOutput.from_raw("[1, 2, 3]")
    # The envelope extractor finds `{` to `}` — for an array, neither
    # exists, so we get empty. (If a future change found an inner object
    # inside an array, the schema would still reject non-dicts.)
    assert out.intent == ""


# ── Round trip ──────────────────────────────────────────────────────────────

def test_round_trip_preserves_fields():
    original = ReaderOutput(
        intent="design a logo",
        constraints=("vector", "two colors"),
        relevant_facts=("brand is playful",),
        proposed_tools=("search", "draft"),
        red_flags=(),
    )
    reparsed = ReaderOutput.from_raw(original.to_json())
    assert reparsed == original
