"""HTML-parsing tests for the Bing image-search response.

We don't hit the network. We feed the parser a representative HTML
fragment and confirm it extracts image URLs from the embedded JSON.
This guards against silent regressions when we change the selector.
"""
import json
from unittest.mock import MagicMock

import pytest

from scraper.parser import Parser


def _make_bing_html(urls):
    """Build a fragment that matches Bing's <a class="iusc" m='{"murl": ...}'>."""
    anchors = "".join(
        f'<a class="iusc" m=\'{json.dumps({"murl": u})}\'></a>'
        for u in urls
    )
    return f"<html><body>{anchors}</body></html>"


@pytest.fixture
def parser(monkeypatch):
    monkeypatch.setenv("DECODO_USERNAME", "u")
    monkeypatch.setenv("DECODO_PASSWORD", "p")
    monkeypatch.setenv("BUCKET", "test-bucket")
    db = MagicMock()
    return Parser(db=db, text="AB123 brake disc", tenant_id="acme")


def test_extracts_murl_from_iusc(parser):
    urls = [
        "https://example.com/a.jpg",
        "https://example.com/b.png",
        "https://example.com/c.webp",
    ]
    resp = MagicMock()
    resp.text = _make_bing_html(urls)
    parser._fetch = lambda use_proxy: resp  # bypass network

    out = parser.get_links()
    assert out == urls


def test_skips_anchors_without_m_attribute(parser):
    html = '<html><body><a class="iusc"></a></body></html>'
    resp = MagicMock()
    resp.text = html
    parser._fetch = lambda use_proxy: resp

    assert parser.get_links() == []


def test_handles_malformed_json_gracefully(parser):
    html = "<html><body><a class=\"iusc\" m='{not-json'></a></body></html>"
    resp = MagicMock()
    resp.text = html
    parser._fetch = lambda use_proxy: resp

    assert parser.get_links() == []


def test_caps_at_thirty_links(parser):
    urls = [f"https://example.com/{i}.jpg" for i in range(60)]
    resp = MagicMock()
    resp.text = _make_bing_html(urls)
    parser._fetch = lambda use_proxy: resp

    assert len(parser.get_links()) == 30


def test_attribute_fallback_when_class_changes(parser):
    # Same payload but the wrapper class is no longer ``iusc``.
    urls = ["https://example.com/a.jpg", "https://example.com/b.jpg"]
    anchors = "".join(
        f'<div class="newclass" m=\'{json.dumps({"murl": u})}\'></div>' for u in urls
    )
    resp = MagicMock()
    resp.text = f"<html><body>{anchors}</body></html>"
    parser._fetch = lambda use_proxy: resp

    assert parser.get_links() == urls


def test_regex_fallback_when_no_attribute_present(parser):
    # Bing has historically also rendered the JSON inline in <script>.
    html = (
        '<html><body><script>var data={"murl":"https://example.com/x.jpg"};'
        '</script><script>var more={"murl":"https://example.com/y.jpg"};</script>'
        "</body></html>"
    )
    resp = MagicMock()
    resp.text = html
    parser._fetch = lambda use_proxy: resp

    assert parser.get_links() == [
        "https://example.com/x.jpg",
        "https://example.com/y.jpg",
    ]


def test_deduplicates_across_fallbacks(parser):
    url = "https://example.com/same.jpg"
    html = (
        f'<a class="iusc" m=\'{json.dumps({"murl": url})}\'></a>'
        f'<div m=\'{json.dumps({"murl": url})}\'></div>'
    )
    resp = MagicMock()
    resp.text = f"<html><body>{html}</body></html>"
    parser._fetch = lambda use_proxy: resp

    assert parser.get_links() == [url]
