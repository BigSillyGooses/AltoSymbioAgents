"""
services/web_research.py — live web fetch for the Researcher agent.

Gives AltoSymbioAgents its first outbound web capability: fetch a public URL,
return clean LLM-friendly markdown, and (via core/api/web.WebAPI) feed it into
the existing RAG index so the Researcher can answer from live pages.

Design notes
------------
* **Lean by default, heavy path opt-in.** The default fetch path uses
  ``curl_cffi`` (the same HTTP engine Scrapling's ``Fetcher`` wraps) for the
  request and Scrapling's parser-only ``Selector`` for extraction. Both ship as
  prebuilt wheels, so the shipped bundle stays lean. ``scrapling.fetchers``
  imports Playwright at module load, so we deliberately do NOT import it on the
  default path — the browser/stealth engine is lazily imported only when the
  user opts in (see ``stealth_available`` / ``use_stealth``).
* **Lazy imports.** Every third-party import happens inside a function so this
  module imports cleanly even when the optional deps are absent; callers probe
  ``is_available()`` first (mirrors ``services/semantic_search.is_available``).
* **Fail closed on security, fail open on UX.** ``_validate_url`` rejects unsafe
  targets (SSRF) before any socket opens and re-checks every redirect hop. A
  rejected or failed fetch raises a typed ``WebFetchError`` the caller turns
  into a friendly message — it never crashes the chat turn.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

log = logging.getLogger("altosybioagents.web_research")

# Only these schemes are ever contacted. Everything else fails closed. We do
# not restrict the port: the real SSRF protection is IP screening below (which
# blocks loopback/LAN/metadata regardless of port), and a hard 80/443-only rule
# would needlessly break legitimate public sites served on other ports.
_ALLOWED_SCHEMES = frozenset({"http", "https"})

# Hard ceilings so a hostile or huge page can't hang or OOM the sidecar. These
# are defaults; settings can lower them but never disable them.
_DEFAULT_TIMEOUT = 20.0
_DEFAULT_MAX_BYTES = 2_000_000
_MAX_REDIRECTS = 5


class WebFetchError(Exception):
    """A web fetch was refused or failed.

    ``reason`` is a stable machine code (``blocked_scheme``, ``blocked_host``,
    ``timeout``, ``transport``, ``too_many_redirects``, ``stealth_unavailable``,
    ``unavailable`` …) for callers/tests to branch on; ``message`` is a short
    human string. The UI maps these to friendly toasts — reason codes are never
    shown to users.
    """

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class FetchResult:
    url: str            # final URL after any redirects
    title: str
    markdown: str       # clean, LLM-friendly markdown
    text: str           # plain-text fallback (Selector.get_all_text())
    status: int
    fetched_at: str     # ISO-8601 UTC
    engine: str         # "http" | "stealth"
    truncated: bool


# ── Capability probes ────────────────────────────────────────────────────────

def is_available() -> bool:
    """True if the default (HTTP) fetch path can run in this environment."""
    try:
        import curl_cffi.requests  # noqa: F401
        from scrapling.parser import Selector  # noqa: F401
        return True
    except Exception:  # noqa: BLE001 — any import failure means unavailable
        return False


def stealth_available() -> bool:
    """True if the opt-in browser/stealth engine is installed.

    Importing ``scrapling.fetchers`` pulls Playwright, so this is the probe the
    UI uses before offering the advanced (JS/anti-bot) path.
    """
    try:
        from scrapling.fetchers import StealthyFetcher  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


# ── SSRF guard ───────────────────────────────────────────────────────────────

def _ip_is_safe_public(ip: ipaddress._BaseAddress) -> bool:
    """False for any address that could reach loopback / LAN / metadata."""
    # Unwrap IPv4-mapped IPv6 (e.g. ::ffff:127.0.0.1) before classifying.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return not (
        ip.is_loopback
        or ip.is_link_local      # 169.254/16 — includes cloud metadata 169.254.169.254
        or ip.is_private         # 10/8, 172.16/12, 192.168/16, fc00::/7
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _domain_lists(settings):
    allowed, blocked = [], []
    if settings is not None:
        try:
            allowed = [str(d).lower().strip() for d in (settings.get("web_research_allowed_domains") or [])]
            blocked = [str(d).lower().strip() for d in (settings.get("web_research_blocked_domains") or [])]
        except Exception:  # noqa: BLE001 — a malformed setting must not crash the fetch
            allowed, blocked = [], []
    return [d for d in allowed if d], [d for d in blocked if d]


def _allow_private(settings) -> bool:
    """Whether loopback/LAN/metadata targets are permitted (default: no)."""
    if settings is None:
        return False
    try:
        return bool(settings.get("web_research_allow_private", False))
    except Exception:  # noqa: BLE001
        return False


def _host_matches(host: str, domain: str) -> bool:
    """True if ``host`` equals ``domain`` or is a subdomain of it."""
    host = host.lower().rstrip(".")
    return host == domain or host.endswith("." + domain)


def _validate_url(url: str, settings=None) -> str:
    """Validate a URL against the SSRF policy. Returns the cleaned URL or raises.

    Fails closed: a resolution error, an unexpected scheme/port, or any resolved
    IP that is not safely public results in ``WebFetchError``. Called before the
    first request and again on every redirect target.
    """
    if not isinstance(url, str) or not url.strip():
        raise WebFetchError("invalid_url", "No URL was provided.")
    parts = urlsplit(url.strip())

    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise WebFetchError("blocked_scheme", f"Only http/https URLs are allowed (got {parts.scheme!r}).")

    host = parts.hostname
    if not host:
        raise WebFetchError("invalid_url", "The URL has no host.")

    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)

    allowed, blocked = _domain_lists(settings)
    if any(_host_matches(host, d) for d in blocked):
        raise WebFetchError("blocked_host", "That site is on the blocked list.")
    if allowed and not any(_host_matches(host, d) for d in allowed):
        raise WebFetchError("blocked_host", "That site is not on the allowed list.")

    # Resolve and screen EVERY address (defends against a hostname that resolves
    # to a private IP, and against IPv6/IPv4 split results). The screen is
    # skipped only when the user has explicitly opted into private targets.
    allow_private = _allow_private(settings)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise WebFetchError("dns", f"Could not resolve {host}.") from exc
    if not infos:
        raise WebFetchError("dns", f"Could not resolve {host}.")
    if not allow_private:
        for info in infos:
            sockaddr = info[4]
            try:
                ip = ipaddress.ip_address(sockaddr[0])
            except ValueError:
                raise WebFetchError("blocked_host", "Resolved an unreadable address.")
            if not _ip_is_safe_public(ip):
                raise WebFetchError("blocked_host", "That address is private or internal — refused for safety.")

    return url.strip()


# ── Fetch ────────────────────────────────────────────────────────────────────

def _extract(html: str, final_url: str, status: int, *, engine: str,
             max_bytes: int) -> FetchResult:
    """Parse fetched HTML into title + markdown + text, capped at ``max_bytes``."""
    from scrapling.parser import Selector  # lazy
    import markdownify  # lazy

    truncated = False
    if len(html) > max_bytes:
        html = html[:max_bytes]
        truncated = True

    title = ""
    text = ""
    try:
        sel = Selector(html)
        title = (sel.css("title::text").get() or "").strip()
        text = sel.get_all_text() or ""
    except Exception as exc:  # noqa: BLE001 — never fail the fetch on a parse hiccup
        log.debug("web_research: Selector parse failed for %s: %s", final_url, exc)

    try:
        markdown = markdownify.markdownify(html).strip()
    except Exception as exc:  # noqa: BLE001
        log.debug("web_research: markdownify failed for %s: %s", final_url, exc)
        markdown = text  # plain-text fallback

    return FetchResult(
        url=final_url,
        title=title,
        markdown=markdown,
        text=text,
        status=status,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        engine=engine,
        truncated=truncated,
    )


async def _fetch_http(url: str, *, timeout: float, max_bytes: int, settings) -> FetchResult:
    """Default lean path: curl_cffi request + Scrapling Selector parse.

    Redirects are followed manually so every hop is re-validated against the
    SSRF policy (a public URL must not be able to bounce us into the LAN).
    """
    from curl_cffi.requests import AsyncSession  # lazy

    current = _validate_url(url, settings)
    async with AsyncSession() as session:
        for _hop in range(_MAX_REDIRECTS + 1):
            try:
                resp = await session.get(
                    current,
                    impersonate="chrome",
                    timeout=timeout,
                    allow_redirects=False,
                    max_recv_speed=0,
                )
            except Exception as exc:  # noqa: BLE001 — curl errors → typed error
                msg = str(exc).lower()
                if "timed out" in msg or "timeout" in msg:
                    raise WebFetchError("timeout", "That page took too long to respond.") from exc
                raise WebFetchError("transport", "Couldn't reach that page.") from exc

            status = resp.status_code
            if status in (301, 302, 303, 307, 308):
                location = resp.headers.get("location") or resp.headers.get("Location")
                if not location:
                    raise WebFetchError("transport", "The site sent a redirect with no destination.")
                # Resolve relative redirects against the current URL.
                from urllib.parse import urljoin
                current = _validate_url(urljoin(current, location), settings)
                continue

            return _extract(resp.text, current, status, engine="http", max_bytes=max_bytes)

    raise WebFetchError("too_many_redirects", "That page redirected too many times.")


async def _fetch_stealth(url: str, *, timeout: float, max_bytes: int, settings) -> FetchResult:
    """Opt-in heavy path: Scrapling's browser-based StealthyFetcher.

    Lazily imported so a missing Playwright/browser never breaks the default
    path or this module's import.
    """
    current = _validate_url(url, settings)
    try:
        from scrapling.fetchers import StealthyFetcher  # lazy; pulls Playwright
    except Exception as exc:  # noqa: BLE001
        raise WebFetchError(
            "stealth_unavailable",
            "Advanced web fetching isn't installed yet.",
        ) from exc
    try:
        import anyio
        page = await anyio.to_thread.run_sync(
            lambda: StealthyFetcher.fetch(current, headless=True, network_idle=True)
        )
        html = getattr(page, "html_content", None) or getattr(page, "body", "") or str(page)
        status = int(getattr(page, "status", 200) or 200)
    except WebFetchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise WebFetchError("transport", "Couldn't load that page with advanced fetching.") from exc
    return _extract(html, current, status, engine="stealth", max_bytes=max_bytes)


async def fetch_url(
    url: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    use_stealth: bool = False,
    settings=None,
) -> FetchResult:
    """Fetch ``url`` and return clean markdown/text, or raise ``WebFetchError``.

    Validates the URL (SSRF guard) before any network call and re-validates
    every redirect hop. Never raises raw library exceptions to the caller.
    """
    if not is_available():
        raise WebFetchError("unavailable", "Web research isn't available in this build.")

    # Clamp the resource bounds so a caller can't widen them past the ceilings.
    timeout = float(min(max(timeout, 1.0), 60.0))
    max_bytes = int(min(max(max_bytes, 1024), 10_000_000))

    if use_stealth:
        return await _fetch_stealth(url, timeout=timeout, max_bytes=max_bytes, settings=settings)
    return await _fetch_http(url, timeout=timeout, max_bytes=max_bytes, settings=settings)


# ── URL extraction + sync fetch-and-index (for the sync orchestrator path) ────

import re as _re

# Plain http(s) URLs; trailing punctuation is trimmed so "see https://x.com."
# doesn't capture the period.
_URL_RE = _re.compile(r"https?://[^\s<>\"')\]]+", _re.IGNORECASE)


def extract_urls(text: str, *, limit: int = 3) -> list[str]:
    """Return up to ``limit`` distinct http(s) URLs found in ``text``."""
    if not text:
        return []
    out: list[str] = []
    for m in _URL_RE.finditer(text):
        url = m.group(0).rstrip(".,;:!?")
        if url not in out:
            out.append(url)
        if len(out) >= limit:
            break
    return out


def fetch_and_index(url: str, *, rag, settings, source: str = "") -> dict:
    """Synchronously fetch ``url``, security-scan it, and index it into ``rag``.

    Sync wrapper for callers outside an event loop (the chat orchestrator runs
    in a worker thread). Best-effort: returns ``{"error", "reason"}`` rather
    than raising so a bad URL can never break a chat turn. ``rag`` is any object
    exposing ``add_text(text, source=, doc_type=)``.
    """
    import asyncio

    from services import input_sanitizer

    try:
        result = asyncio.run(fetch_url(url, settings=settings))
    except WebFetchError as exc:
        return {"error": exc.message, "reason": exc.reason}
    except Exception as exc:  # noqa: BLE001
        log.debug("fetch_and_index: fetch failed for %s: %s", url, exc)
        return {"error": "fetch failed", "reason": "transport"}

    content = result.markdown or result.text
    if not content.strip():
        return {"error": "empty page", "reason": "empty"}
    try:
        scan = input_sanitizer.scan_document(content, filename=result.url)
    except Exception as exc:  # noqa: BLE001
        log.debug("fetch_and_index: scan failed for %s: %s", result.url, exc)
        return {"error": "scan failed", "reason": "scan_failed"}
    if scan.get("blocked"):
        return {"error": "blocked by security scan", "reason": "blocked"}

    try:
        n = rag.add_text(content, source=source or result.url, doc_type="web")
    except Exception as exc:  # noqa: BLE001
        log.debug("fetch_and_index: index failed for %s: %s", result.url, exc)
        return {"error": "index failed", "reason": "index_failed"}
    return {"chunks_added": n, "url": result.url, "title": result.title}
