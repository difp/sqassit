"""Тесты извлечения VLESS-ссылок из subscription-ответа."""

from __future__ import annotations

import base64
import json

from scripts.update_xray_config import extract_vless_links

LINK1 = (
    "vless://11111111-2222-3333-4444-555555555555@example.com:443"
    "?type=tcp&security=tls&sni=example.com#Server-1"
)
LINK2 = "vless://aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee@example.net:443?type=ws#Server-2"


def test_plain_text():
    links = extract_vless_links(f"header\n{LINK1}\nfooter")
    assert links == [LINK1]


def test_base64_standard():
    raw = base64.b64encode(f"{LINK1}\n{LINK2}".encode()).decode()
    assert extract_vless_links(raw) == [LINK1, LINK2]


def test_base64_urlsafe_without_padding():
    raw = base64.urlsafe_b64encode(LINK2.encode()).decode().rstrip("=")
    assert extract_vless_links(raw) == [LINK2]


def test_json_nested():
    raw = json.dumps({"data": {"servers": [{"url": LINK1}]}})
    assert extract_vless_links(raw) == [LINK1]


def test_html_escaped_ampersands():
    raw = f"<html><body>{LINK1.replace('&', '&amp;')}</body></html>"
    assert extract_vless_links(raw) == [LINK1]


def test_deduplicates():
    assert extract_vless_links(f"{LINK1}\n{LINK1}\n{LINK2}") == [LINK1, LINK2]


def test_no_links():
    assert extract_vless_links("not a subscription") == []
