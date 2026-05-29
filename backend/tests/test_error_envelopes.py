"""Stage-2 #11 — typed error envelopes.

Covers two seams:

1.  The :class:`DomainError` constructors return the right discriminator +
    status code + message + hint shape. Each constructor is one classmethod
    on a closed catalog, so a regression here means a renderer switch case
    silently loses its target.
2.  The production :func:`install_error_handlers` registers handlers that
    serialise both :class:`DomainError` raises and plain ``HTTPException``
    raises into the same envelope shape. The HTTPException wrapper lets
    unmigrated routes keep working without breaking the renderer's
    discriminated-union parser.

The integration tests register the **real** production handlers on a fresh
FastAPI app (via ``install_error_handlers``), attach throwaway probe routes
that raise real DomainError / HTTPException values, and hit them through
the real ASGI request pipeline. No mocks, no copied handler bodies.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from core.errors import DomainError, http_exception_to_envelope, install_error_handlers


# ── Unit: DomainError constructors ───────────────────────────────────────────


def test_conversation_not_found_carries_id_as_hint() -> None:
    err = DomainError.conversation_not_found("c-42")
    assert err.error_type == "conversation_not_found"
    assert err.status_code == 404
    assert err.message == "Conversation not found"
    assert err.hint == "id=c-42"


def test_conversation_not_found_without_id_has_no_hint() -> None:
    err = DomainError.conversation_not_found()
    assert err.error_type == "conversation_not_found"
    assert err.status_code == 404
    assert err.hint is None


def test_attachment_not_found_distinguishes_missing_file() -> None:
    a = DomainError.attachment_not_found()
    b = DomainError.attachment_not_found(missing_file=True)
    assert a.error_type == "attachment_not_found"
    assert a.status_code == 404
    assert a.message == "Attachment not found."
    # Same discriminator, different message — so the renderer can show the
    # right hint without a second status code.
    assert b.error_type == "attachment_not_found"
    assert b.status_code == 404
    assert b.message == "Attachment file missing."


def test_attachment_invalid_uses_400_and_passes_message_through() -> None:
    err = DomainError.attachment_invalid("Empty file.")
    assert err.error_type == "attachment_invalid"
    assert err.status_code == 400
    assert err.message == "Empty file."


def test_voice_invalid_input_uses_400() -> None:
    err = DomainError.voice_invalid_input("text is empty")
    assert err.error_type == "voice_invalid_input"
    assert err.status_code == 400
    assert err.message == "text is empty"


def test_voice_engine_unavailable_uses_503() -> None:
    err = DomainError.voice_engine_unavailable("Whisper model not downloaded")
    assert err.error_type == "voice_engine_unavailable"
    assert err.status_code == 503


def test_rag_unavailable_uses_503() -> None:
    err = DomainError.rag_unavailable("RAG index is unavailable")
    assert err.error_type == "rag_unavailable"
    assert err.status_code == 503


def test_attachment_save_failed_uses_500() -> None:
    err = DomainError.attachment_save_failed("Disk full")
    assert err.error_type == "attachment_save_failed"
    assert err.status_code == 500


def test_invalid_search_query_uses_400() -> None:
    err = DomainError.invalid_search_query()
    assert err.error_type == "invalid_search_query"
    assert err.status_code == 400


def test_prompt_template_not_found_uses_404() -> None:
    err = DomainError.prompt_template_not_found()
    assert err.error_type == "prompt_template_not_found"
    assert err.status_code == 404


def test_internal_error_default_message() -> None:
    err = DomainError.internal_error()
    assert err.error_type == "internal_error"
    assert err.status_code == 500
    assert err.message == "Internal server error"


def test_to_dict_emits_all_four_envelope_fields() -> None:
    err = DomainError.conversation_not_found("c-1")
    payload = err.to_dict()
    assert set(payload.keys()) == {"error_type", "status_code", "message", "hint"}
    assert payload["error_type"] == "conversation_not_found"
    assert payload["status_code"] == 404
    assert payload["message"] == "Conversation not found"
    assert payload["hint"] == "id=c-1"


# ── Unit: HTTPException → envelope wrapper ───────────────────────────────────


def test_http_exception_envelope_wraps_string_detail() -> None:
    body = http_exception_to_envelope(404, "Conversation not found")
    assert body == {
        "error_type":  "http_error",
        "status_code": 404,
        "message":     "Conversation not found",
        "hint":        None,
    }


def test_http_exception_envelope_stringifies_non_string_detail() -> None:
    # FastAPI lets routes pass dicts as ``detail`` — we don't lose the data,
    # but we do flatten to a string so the renderer always reads `message`
    # as a string.
    body = http_exception_to_envelope(422, {"field": "name", "reason": "missing"})
    assert body["error_type"] == "http_error"
    assert body["status_code"] == 422
    assert "missing" in body["message"]


# ── Integration: real handlers installed via install_error_handlers ──────────


def _app_with_real_handlers() -> FastAPI:
    """A FastAPI app with the PRODUCTION handlers installed and three throwaway
    probe routes that raise real DomainError / HTTPException values.

    No mocks. The handlers under test are the same ones :func:`build_app`
    registers on the production sidecar — :func:`install_error_handlers`
    is shared, so any drift in the production glue surfaces here.
    """
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/probe/domain-error")
    async def _probe_domain() -> dict:
        raise DomainError.conversation_not_found("c-99")

    @app.get("/probe/http-error")
    async def _probe_http() -> dict:
        raise HTTPException(status_code=403, detail="forbidden")

    @app.get("/probe/ok")
    async def _probe_ok() -> dict:
        return {"ok": True}

    return app


def test_domain_error_handler_emits_typed_envelope() -> None:
    client = TestClient(_app_with_real_handlers())
    resp = client.get("/probe/domain-error")
    assert resp.status_code == 404
    body = resp.json()
    assert body == {
        "error_type":  "conversation_not_found",
        "status_code": 404,
        "message":     "Conversation not found",
        "hint":        "id=c-99",
    }


def test_http_exception_handler_wraps_into_same_envelope_shape() -> None:
    client = TestClient(_app_with_real_handlers())
    resp = client.get("/probe/http-error")
    assert resp.status_code == 403
    body = resp.json()
    # Same set of keys as DomainError so the renderer's parser doesn't have
    # to branch on response shape.
    assert set(body.keys()) == {"error_type", "status_code", "message", "hint"}
    assert body["error_type"] == "http_error"
    assert body["message"] == "forbidden"
    assert body["hint"] is None


def test_handlers_do_not_swallow_normal_responses() -> None:
    client = TestClient(_app_with_real_handlers())
    resp = client.get("/probe/ok")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
