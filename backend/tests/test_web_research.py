"""tests/test_web_research.py — real fetch + parse for services/web_research.

These tests run the genuine curl_cffi fetch + Scrapling Selector parse path
against the real ``local_web_server`` fixture (a real HTTP server on loopback).
No mocked fetchers, no canned FetchResults. Loopback is reached by opting into
``web_research_allow_private`` on a real Settings instance, exactly as a user
indexing an internal docs server would.
"""

from __future__ import annotations

import pytest

from services import web_research as wr


@pytest.fixture
def priv_settings(settings):
    """Real Settings with the private-target opt-in on (to reach loopback)."""
    settings.set("web_research_allow_private", True)
    return settings


def test_is_available_true_with_deps_installed():
    # The lean deps (curl_cffi, scrapling parser, markdownify) ship in
    # requirements.txt, so the default path must report available in CI.
    assert wr.is_available() is True


def test_stealth_available_is_bool():
    # Playwright is intentionally NOT a shipped dep; this must not raise and
    # must return a plain bool either way.
    assert isinstance(wr.stealth_available(), bool)


@pytest.mark.asyncio
async def test_fetch_real_page_extracts_title_markdown_text(local_web_server, priv_settings):
    res = await wr.fetch_url(local_web_server.url("/"), settings=priv_settings)

    assert res.status == 200
    assert res.engine == "http"
    assert res.title == "Acme Widgets"           # whitespace stripped
    assert "Widget 3000" in res.markdown          # real HTML→markdown
    assert "$42" in res.markdown
    assert "Widget 3000" in res.text              # plain-text fallback present
    assert res.truncated is False
    assert res.url == local_web_server.url("/")


@pytest.mark.asyncio
async def test_fetch_truncates_oversized_page(local_web_server, priv_settings):
    res = await wr.fetch_url(local_web_server.url("/big"), settings=priv_settings, max_bytes=2048)
    assert res.truncated is True
    # markdown is derived from the truncated HTML, so it stays bounded
    assert len(res.markdown) < 50_000


@pytest.mark.asyncio
async def test_fetch_follows_safe_redirect(local_web_server, priv_settings):
    # /redir 302s to / on the same server; the final content is the article.
    res = await wr.fetch_url(local_web_server.url("/redir"), settings=priv_settings)
    assert res.status == 200
    assert res.title == "Acme Widgets"
    assert res.url == local_web_server.url("/")


@pytest.mark.asyncio
async def test_redirect_loop_is_bounded(local_web_server, priv_settings):
    with pytest.raises(wr.WebFetchError) as ei:
        await wr.fetch_url(local_web_server.url("/loop"), settings=priv_settings)
    assert ei.value.reason == "too_many_redirects"


@pytest.mark.asyncio
async def test_timeout_surfaces_as_web_fetch_error(local_web_server, priv_settings):
    with pytest.raises(wr.WebFetchError) as ei:
        await wr.fetch_url(local_web_server.url("/slow"), settings=priv_settings, timeout=1.0)
    assert ei.value.reason in {"timeout", "transport"}


@pytest.mark.asyncio
async def test_unavailable_raises(monkeypatch):
    monkeypatch.setattr(wr, "is_available", lambda: False)
    with pytest.raises(wr.WebFetchError) as ei:
        await wr.fetch_url("https://example.com", settings=None)
    assert ei.value.reason == "unavailable"


def test_extract_urls_finds_and_trims():
    text = "See https://example.com/a, and (https://example.org/b). No url here."
    assert wr.extract_urls(text) == ["https://example.com/a", "https://example.org/b"]


def test_extract_urls_dedupes_and_caps():
    text = "https://x.com https://x.com https://y.com https://z.com https://w.com"
    out = wr.extract_urls(text, limit=2)
    assert out == ["https://x.com", "https://y.com"]


def test_extract_urls_empty():
    assert wr.extract_urls("") == []
    assert wr.extract_urls("nothing to see") == []
