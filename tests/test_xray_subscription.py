"""Тесты извлечения VLESS-ссылок из subscription-ответа."""

from __future__ import annotations

import base64
import grp
import json
import os
import pwd
from types import SimpleNamespace

from scripts import update_xray_config
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


def test_service_owner_from_systemd(monkeypatch):
    user = pwd.getpwuid(os.getuid())
    group = grp.getgrgid(os.getgid())
    monkeypatch.setattr(
        update_xray_config.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=f"User={user.pw_name}\nGroup={group.gr_name}\n",
        ),
    )
    assert update_xray_config._service_owner("xray") == (user.pw_uid, group.gr_gid)


def test_service_owner_empty_user_is_root(monkeypatch):
    monkeypatch.setattr(
        update_xray_config.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="User=\nGroup=\n",
        ),
    )
    root = pwd.getpwnam("root")
    assert update_xray_config._service_owner("xray") == (root.pw_uid, root.pw_gid)


def test_service_owner_ignores_systemctl_errors(monkeypatch):
    monkeypatch.setattr(
        update_xray_config.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=""),
    )
    assert update_xray_config._service_owner("xray") is None
