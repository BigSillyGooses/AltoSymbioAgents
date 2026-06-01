"""
core/api/web.py — live web-research bridge methods.

Wraps services/web_research with the app's safety + RAG plumbing:
  * ``web_status``        — capability probe for the UI (available / stealth / on).
  * ``web_fetch``         — fetch a URL → clean markdown (no indexing).
  * ``web_fetch_to_rag``  — fetch → security scan → index into the existing RAG
                            so the Researcher's hybrid search picks it up.

Everything is gated on the ``web_research_enabled`` setting (default OFF), so
with the feature off none of this runs. Fetched content is untrusted, so it
routes through ``input_sanitizer.scan_document`` exactly like ``rag_add_file``
before it can reach the index (indirect-prompt-injection defence).
"""

from __future__ import annotations

from core import paths

from services import input_sanitizer, web_research

from ._base import BaseAPI


class WebAPI(BaseAPI):

    # ── Capability probe ─────────────────────────────────────────────────────

    def web_status(self) -> dict:
        """Report whether web research can run and whether it's switched on."""
        try:
            enabled = bool(self._settings.get("web_research_enabled", False))
        except Exception:  # noqa: BLE001
            enabled = False
        return {
            "available": web_research.is_available(),
            "stealth_available": web_research.stealth_available(),
            "enabled": enabled,
        }

    # ── Fetch ────────────────────────────────────────────────────────────────

    def _enabled(self) -> bool:
        try:
            return bool(self._settings.get("web_research_enabled", False))
        except Exception:  # noqa: BLE001
            return False

    def _bounds(self) -> tuple[float, int]:
        try:
            timeout = float(self._settings.get("web_research_timeout_s", 20) or 20)
        except Exception:  # noqa: BLE001
            timeout = 20.0
        try:
            max_bytes = int(self._settings.get("web_research_max_bytes", 2_000_000) or 2_000_000)
        except Exception:  # noqa: BLE001
            max_bytes = 2_000_000
        return timeout, max_bytes

    async def web_fetch(self, url: str, use_stealth: bool = False) -> dict:
        """Fetch a URL and return its markdown. Does not touch the RAG index."""
        if not self._enabled():
            return {"error": "Web research is turned off.", "reason": "disabled"}
        timeout, max_bytes = self._bounds()
        try:
            result = await web_research.fetch_url(
                url,
                timeout=timeout,
                max_bytes=max_bytes,
                use_stealth=use_stealth,
                settings=self._settings,
            )
        except web_research.WebFetchError as exc:
            return {"error": exc.message, "reason": exc.reason}
        except Exception as exc:  # noqa: BLE001 — never leak a raw error to the route
            self._log.warning("web_fetch failed for %r: %s", url, exc)
            return {"error": "Couldn't open that page.", "reason": "transport"}
        return {
            "url": result.url,
            "title": result.title,
            "markdown": result.markdown,
            "text": result.text,
            "status": result.status,
            "engine": result.engine,
            "truncated": result.truncated,
        }

    async def web_fetch_to_rag(self, url: str, source: str = "",
                               use_stealth: bool = False) -> dict:
        """Fetch a URL, security-scan it, and index it into the RAG store."""
        if not self._enabled():
            return {"error": "Web research is turned off.", "reason": "disabled"}
        if self._rag is None:
            return {"error": "The knowledge index isn't ready yet.", "reason": "rag_unavailable"}

        timeout, max_bytes = self._bounds()
        self._emit("web_fetch", {"status": "fetching", "url": url})
        try:
            result = await web_research.fetch_url(
                url,
                timeout=timeout,
                max_bytes=max_bytes,
                use_stealth=use_stealth,
                settings=self._settings,
            )
        except web_research.WebFetchError as exc:
            self._emit("web_fetch", {"status": "error", "url": url, "error": exc.message})
            return {"error": exc.message, "reason": exc.reason}
        except Exception as exc:  # noqa: BLE001
            self._log.warning("web_fetch_to_rag fetch failed for %r: %s", url, exc)
            self._emit("web_fetch", {"status": "error", "url": url, "error": "fetch failed"})
            return {"error": "Couldn't open that page.", "reason": "transport"}

        # Untrusted content — scan before it can reach the index. Fail closed.
        content = result.markdown or result.text
        try:
            scan = input_sanitizer.scan_document(content, filename=result.url)
        except Exception as exc:  # noqa: BLE001
            self._log.warning("web_fetch_to_rag scan failed for %s: %s", result.url, exc)
            self._emit("web_fetch", {"status": "error", "url": result.url, "error": "scan failed"})
            return {"error": "Security scan failed; refusing to index that page.", "reason": "scan_failed"}
        if scan.get("blocked"):
            self._emit("web_fetch", {"status": "blocked", "url": result.url})
            return {
                "error": "That page was blocked by the security scan — it may contain injected instructions.",
                "reason": "blocked",
                "scan_id": scan.get("scan_id"),
            }

        try:
            n = self._rag.add_text(content, source=source or result.url, doc_type="web")
            if n:
                self._rag.save(paths.rag_cache_dir() / "index.npz")
        except Exception as exc:  # noqa: BLE001
            self._log.warning("web_fetch_to_rag index failed for %s: %s", result.url, exc)
            self._emit("web_fetch", {"status": "error", "url": result.url, "error": "index failed"})
            return {"error": "Couldn't add that page to the knowledge index.", "reason": "index_failed"}

        self._emit("web_fetch", {"status": "done", "url": result.url, "title": result.title})
        self._emit("rag_done", {"chunks": n, "url": result.url})
        return {
            "chunks_added": n,
            "url": result.url,
            "title": result.title,
            "truncated": result.truncated,
        }
