"""tests/test_web_research_ssrf.py — adversarial SSRF matrix for web_research.

Exercises the real ``_validate_url`` (real ``ipaddress`` classification, real
DNS resolution for public/loopback names) against a real Settings instance — no
monkeypatched resolver, no stub settings. Every unsafe target must raise
``WebFetchError`` and fail closed.
"""

from __future__ import annotations

import pytest

from services import web_research as wr


def test_blocks_non_http_schemes(settings):
    for bad in ("file:///etc/passwd", "ftp://host/x", "gopher://h/", "data:text/html,hi"):
        with pytest.raises(wr.WebFetchError) as ei:
            wr._validate_url(bad, settings)
        assert ei.value.reason == "blocked_scheme"


def test_blocks_loopback_and_localhost(settings):
    for bad in ("http://127.0.0.1/", "http://127.0.0.5/", "http://localhost/", "http://[::1]/"):
        with pytest.raises(wr.WebFetchError) as ei:
            wr._validate_url(bad, settings)
        assert ei.value.reason == "blocked_host"


def test_blocks_private_ranges(settings):
    for bad in ("http://10.0.0.5/", "http://192.168.1.1/", "http://172.16.0.9/"):
        with pytest.raises(wr.WebFetchError) as ei:
            wr._validate_url(bad, settings)
        assert ei.value.reason == "blocked_host"


def test_blocks_cloud_metadata_endpoint(settings):
    with pytest.raises(wr.WebFetchError) as ei:
        wr._validate_url("http://169.254.169.254/latest/meta-data/", settings)
    assert ei.value.reason == "blocked_host"


def test_blocks_ipv4_mapped_ipv6_loopback(settings):
    with pytest.raises(wr.WebFetchError) as ei:
        wr._validate_url("http://[::ffff:127.0.0.1]/", settings)
    assert ei.value.reason == "blocked_host"


def test_blocks_no_host(settings):
    with pytest.raises(wr.WebFetchError):
        wr._validate_url("http:///nopath", settings)


def test_blocklist_denies_domain_and_subdomain(settings):
    settings.set("web_research_blocked_domains", ["evil.example"])
    for bad in ("http://evil.example/", "http://api.evil.example/x"):
        with pytest.raises(wr.WebFetchError) as ei:
            wr._validate_url(bad, settings)
        assert ei.value.reason == "blocked_host"


def test_allowlist_denies_unlisted_domain(settings):
    settings.set("web_research_allowed_domains", ["good.example"])
    with pytest.raises(wr.WebFetchError) as ei:
        wr._validate_url("http://other.example/", settings)
    assert ei.value.reason == "blocked_host"


def test_allow_private_opt_in_permits_loopback(settings):
    # With the explicit opt-in, the IP screen is skipped (host still resolves).
    settings.set("web_research_allow_private", True)
    out = wr._validate_url("http://127.0.0.1:8080/docs", settings)
    assert out == "http://127.0.0.1:8080/docs"
