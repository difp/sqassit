#!/usr/bin/env python3
"""
Генерирует конфиг Xray-core из VLESS-ссылки.

На выходе — SOCKS5 на 127.0.0.1:1080 и VLESS outbound. Приложение затем
использует `BOT_PROXY=socks5://127.0.0.1:1080`.

Поддерживаются распространённые варианты:
  * VLESS + Reality (type=tcp, security=reality);
  * VLESS + TLS (type=tcp/ws/grpc/http);
  * без TLS (security=none);
  * flow=xtls-rprx-vision;
  * ws path/host, grpc serviceName, tcp headerType=http.

Пример:
    python3 scripts/xray_vless.py 'vless://uuid@host:443?...' \
        --write /usr/local/etc/xray/config.json

Не коммитьте сгенерированный конфиг и не публикуйте VLESS-ссылку.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


class VlessError(ValueError):
    """Некорректная или неподдерживаемая VLESS-ссылка."""


def _first(values: dict[str, list[str]], key: str, default: str = "") -> str:
    items = values.get(key)
    return items[-1] if items else default


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_vless(url: str) -> dict[str, Any]:
    """Разбирает VLESS-URI в нормализованный словарь."""
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() != "vless":
        raise VlessError("Ожидается ссылка vless://...")
    if not parsed.hostname:
        raise VlessError("В ссылке нет хоста.")
    uuid = unquote(parsed.username or "")
    if not uuid:
        raise VlessError("В ссылке нет UUID.")

    query = parse_qs(parsed.query, keep_blank_values=True)
    network = (_first(query, "type", "tcp") or "tcp").lower()
    security = (_first(query, "security", "none") or "none").lower()
    host = parsed.hostname
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise VlessError(f"Некорректный порт: {parsed.netloc}") from exc
    server_name = (
        _first(query, "sni")
        or _first(query, "serverName")
        or _first(query, "host")
        or host
    )
    host_header = _first(query, "host") or server_name
    path = unquote(_first(query, "path", "/") or "/")
    alpn = [item for item in _first(query, "alpn").split(",") if item]

    return {
        "uuid": uuid,
        "host": host,
        "port": port,
        "network": network,
        "security": security,
        "server_name": server_name,
        "host_header": host_header,
        "path": path,
        "service_name": _first(query, "serviceName"),
        "flow": _first(query, "flow"),
        "encryption": _first(query, "encryption", "none") or "none",
        "fingerprint": _first(query, "fp") or "chrome",
        "alpn": alpn,
        "allow_insecure": _bool(_first(query, "allowInsecure")),
        "public_key": _first(query, "pbk"),
        "short_id": _first(query, "sid"),
        "spider_x": _first(query, "spx") or "/",
        "header_type": _first(query, "headerType").lower(),
    }


def _stream_settings(link: dict[str, Any]) -> dict[str, Any]:
    network = link["network"]
    security = link["security"]
    stream: dict[str, Any] = {"network": network, "security": security}

    if security == "tls":
        tls: dict[str, Any] = {"serverName": link["server_name"]}
        if link["fingerprint"]:
            tls["fingerprint"] = link["fingerprint"]
        if link["alpn"]:
            tls["alpn"] = link["alpn"]
        if link["allow_insecure"]:
            tls["allowInsecure"] = True
        stream["tlsSettings"] = tls
    elif security == "reality":
        if not link["public_key"] or not link["short_id"]:
            raise VlessError("Для Reality нужны параметры pbk и sid.")
        stream["realitySettings"] = {
            "serverName": link["server_name"],
            "fingerprint": link["fingerprint"] or "chrome",
            "publicKey": link["public_key"],
            "shortId": link["short_id"],
            "spiderX": link["spider_x"],
        }
    elif security != "none":
        raise VlessError(f"Неподдерживаемый security={security!r}")

    if network == "ws":
        ws: dict[str, Any] = {"path": link["path"]}
        if link["host_header"]:
            ws["headers"] = {"Host": link["host_header"]}
        stream["wsSettings"] = ws
    elif network == "grpc":
        service = link["service_name"] or link["path"].strip("/")
        if not service:
            raise VlessError("Для gRPC нужен serviceName.")
        stream["grpcSettings"] = {"serviceName": service}
    elif network == "http":
        stream["httpSettings"] = {
            "path": link["path"],
            "host": [link["host_header"]] if link["host_header"] else [],
        }
    elif network == "tcp":
        if link["header_type"] == "http":
            stream["tcpSettings"] = {
                "header": {
                    "type": "http",
                    "request": {
                        "path": [link["path"] or "/"],
                        "headers": {"Host": [link["host_header"]]},
                    },
                }
            }
    else:
        raise VlessError(f"Неподдерживаемый type={network!r}")

    return stream


def build_xray_config(url: str, *, socks_port: int = 1080) -> dict[str, Any]:
    """Собирает Xray-конфиг с SOCKS5 inbound и VLESS outbound."""
    link = parse_vless(url)
    user: dict[str, Any] = {
        "id": link["uuid"],
        "encryption": link["encryption"],
    }
    if link["flow"]:
        user["flow"] = link["flow"]

    outbound = {
        "tag": "vless-out",
        "protocol": "vless",
        "settings": {
            "vnext": [
                {
                    "address": link["host"],
                    "port": link["port"],
                    "users": [user],
                }
            ]
        },
        "streamSettings": _stream_settings(link),
    }

    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "tag": "socks-in",
                "listen": "127.0.0.1",
                "port": socks_port,
                "protocol": "socks",
                "settings": {"auth": "noauth", "udp": True},
            }
        ],
        "outbounds": [outbound],
    }


def _write_config(path: Path, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Сгенерировать Xray config с SOCKS5 для VLESS-ссылки."
    )
    parser.add_argument("url", help="vless://...")
    parser.add_argument("--socks-port", type=int, default=1080)
    parser.add_argument("--write", type=Path, help="куда записать config.json")
    args = parser.parse_args(argv)

    try:
        config = build_xray_config(args.url, socks_port=args.socks_port)
    except VlessError as exc:
        parser.error(str(exc))

    text = json.dumps(config, ensure_ascii=False, indent=2)
    if args.write:
        _write_config(args.write, config)
        print(f"written: {args.write}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
