"""Typed error envelopes for the FastAPI sidecar.

Stage-2 #11. A ``DomainError`` is a structured exception that route handlers
raise instead of bare ``HTTPException(status_code, detail="…")``. The global
exception handler installed in :mod:`server` serialises it to a JSON envelope
the renderer can pattern-match on ``error_type`` rather than parsing string
messages:

    {
      "error_type":   "conversation_not_found",
      "status_code":  404,
      "message":      "Conversation not found",
      "hint":         null
    }

The same handler wraps any plain ``HTTPException`` into the envelope shape
with ``error_type="http_error"`` so unmigrated routes keep working without
breaking the renderer's parser.

The error-type catalog is a closed set declared as classmethod constructors
on :class:`DomainError`. Adding a new variant means adding a constructor —
the renderer's TypeScript discriminated union can then add the matching
branch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI


class DomainError(Exception):
    """Structured exception with a discriminator the renderer can switch on.

    The body of the HTTP response is built from the four fields below by the
    handler registered in :mod:`server`. Constructors below cover the cases
    that have been migrated so far; new variants should be added as
    classmethods rather than ad-hoc ``DomainError(error_type="…", …)`` calls
    so the set of valid discriminators stays enumerated.
    """

    error_type: str
    status_code: int
    message: str
    hint: str | None

    def __init__(
        self,
        *,
        error_type: str,
        status_code: int,
        message: str,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.status_code = status_code
        self.message = message
        self.hint = hint

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_type":  self.error_type,
            "status_code": self.status_code,
            "message":     self.message,
            "hint":        self.hint,
        }

    # ── 4xx — not found ──────────────────────────────────────────────────

    @classmethod
    def conversation_not_found(cls, conversation_id: str = "") -> "DomainError":
        return cls(
            error_type="conversation_not_found",
            status_code=404,
            message="Conversation not found",
            hint=f"id={conversation_id}" if conversation_id else None,
        )

    @classmethod
    def attachment_not_found(cls, *, missing_file: bool = False) -> "DomainError":
        return cls(
            error_type="attachment_not_found",
            status_code=404,
            message=(
                "Attachment file missing." if missing_file
                else "Attachment not found."
            ),
        )

    @classmethod
    def prompt_template_not_found(cls) -> "DomainError":
        return cls(
            error_type="prompt_template_not_found",
            status_code=404,
            message="prompt template not found",
        )

    # ── 4xx — invalid input ──────────────────────────────────────────────

    @classmethod
    def invalid_search_query(cls) -> "DomainError":
        return cls(
            error_type="invalid_search_query",
            status_code=400,
            message="Invalid search query",
        )

    @classmethod
    def attachment_invalid(cls, message: str) -> "DomainError":
        """Empty upload, unsupported extension, oversize file, etc.

        The renderer surfaces ``message`` verbatim — it's the user-facing
        explanation written in the route handler.
        """
        return cls(
            error_type="attachment_invalid",
            status_code=400,
            message=message,
        )

    @classmethod
    def voice_invalid_input(cls, message: str) -> "DomainError":
        """Empty audio upload, oversize audio, empty text, oversize text."""
        return cls(
            error_type="voice_invalid_input",
            status_code=400,
            message=message,
        )

    # ── 5xx — service / engine ───────────────────────────────────────────

    @classmethod
    def attachment_save_failed(cls, message: str) -> "DomainError":
        return cls(
            error_type="attachment_save_failed",
            status_code=500,
            message=message,
        )

    @classmethod
    def voice_engine_unavailable(cls, message: str) -> "DomainError":
        """The user's STT model or TTS voice isn't downloaded / available.

        Status 503 is the right shape here: the renderer surfaces it as
        "voice is set up but the engine isn't ready yet" and points the
        user at Settings → Voice. A 400 would be misleading — the request
        is well-formed, it's the server-side asset that is missing.
        """
        return cls(
            error_type="voice_engine_unavailable",
            status_code=503,
            message=message,
        )

    @classmethod
    def rag_unavailable(cls, message: str) -> "DomainError":
        return cls(
            error_type="rag_unavailable",
            status_code=503,
            message=message,
        )

    # ── 5xx — internal ───────────────────────────────────────────────────

    @classmethod
    def internal_error(cls, message: str = "Internal server error") -> "DomainError":
        return cls(
            error_type="internal_error",
            status_code=500,
            message=message,
        )


# ── Envelope shape for plain HTTPException pass-through ──────────────────────


def http_exception_to_envelope(status_code: int, detail: Any) -> dict[str, Any]:
    """Wrap an unmigrated ``HTTPException`` into the same envelope shape.

    ``error_type="http_error"`` is the catch-all the renderer falls through
    to when no typed variant matches. Routes that still raise raw
    ``HTTPException(...)`` are not broken — they just don't get the
    discriminator the renderer would otherwise use to special-case them.
    """
    message = detail if isinstance(detail, str) else str(detail)
    return {
        "error_type":  "http_error",
        "status_code": int(status_code),
        "message":     message,
        "hint":        None,
    }


def install_error_handlers(app: "FastAPI") -> None:
    """Register the typed-envelope exception handlers on ``app``.

    Called by :func:`server.build_app` for the production sidecar, and by
    test fixtures that mount a single router on a minimal FastAPI app so
    those tests still observe the same JSON shape the renderer sees.
    """
    from fastapi import HTTPException, Request
    from fastapi.responses import JSONResponse

    @app.exception_handler(DomainError)
    async def _domain_error_handler(
        _request: Request, exc: DomainError,
    ) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(
        _request: Request, exc: HTTPException,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=http_exception_to_envelope(exc.status_code, exc.detail),
        )
