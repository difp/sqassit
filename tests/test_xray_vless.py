"""Тесты генератора Xray-конфига из VLESS-ссылки."""

from __future__ import annotations

import pytest

from scripts.xray_vless import VlessError, build_xray_config

REALITY = (
    "vless://11111111-2222-3333-4444-555555555555@example.com:443"
    "?type=tcp&security=reality&pbk=PUBLIC_KEY&sid=abcd&fp=chrome"
    "&sni=example.com&flow=xtls-rprx-vision#Reality"
)

WS_TLS = (
    "vless://11111111-2222-3333-4444-555555555555@example.com:443"
    "?type=ws&security=tls&sni=cdn.example.com&host=cdn.example.com"
    "&path=%2Fws&fp=chrome#WS"
)


def _outbound(config):
    return config["outbounds"][0]


def test_socks_inbound():
    config = build_xray_config(REALITY, socks_port=1081)
    inbound = config["inbounds"][0]
    assert inbound["protocol"] == "socks"
    assert inbound["listen"] == "127.0.0.1"
    assert inbound["port"] == 1081
    assert inbound["settings"]["udp"] is True


def test_reality_config():
    outbound = _outbound(build_xray_config(REALITY))
    user = outbound["settings"]["vnext"][0]["users"][0]
    assert outbound["protocol"] == "vless"
    assert user["id"] == "11111111-2222-3333-4444-555555555555"
    assert user["flow"] == "xtls-rprx-vision"
    assert outbound["streamSettings"]["security"] == "reality"
    reality = outbound["streamSettings"]["realitySettings"]
    assert reality["publicKey"] == "PUBLIC_KEY"
    assert reality["shortId"] == "abcd"
    assert reality["serverName"] == "example.com"


def test_ws_tls_config():
    outbound = _outbound(build_xray_config(WS_TLS))
    stream = outbound["streamSettings"]
    assert stream["network"] == "ws"
    assert stream["security"] == "tls"
    assert stream["wsSettings"]["path"] == "/ws"
    assert stream["wsSettings"]["headers"]["Host"] == "cdn.example.com"
    assert stream["tlsSettings"]["serverName"] == "cdn.example.com"


def test_grpc_config():
    url = (
        "vless://11111111-2222-3333-4444-555555555555@example.com:443"
        "?type=grpc&security=tls&sni=example.com&serviceName=grpcsvc"
    )
    stream = _outbound(build_xray_config(url))["streamSettings"]
    assert stream["network"] == "grpc"
    assert stream["grpcSettings"]["serviceName"] == "grpcsvc"


def test_no_tls_config():
    url = "vless://11111111-2222-3333-4444-555555555555@example.com:80?type=tcp"
    stream = _outbound(build_xray_config(url))["streamSettings"]
    assert stream["security"] == "none"


def test_reality_requires_keys():
    with pytest.raises(VlessError):
        build_xray_config(
            "vless://11111111-2222-3333-4444-555555555555@example.com:443"
            "?type=tcp&security=reality&sni=example.com"
        )


def test_rejects_non_vless():
    with pytest.raises(VlessError):
        build_xray_config("vmess://example")
