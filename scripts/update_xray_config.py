#!/usr/bin/env python3
"""
Забирает subscription URL, извлекает VLESS-ссылку и обновляет Xray config.

Поддерживаются ответы:
  * plain text с vless://...
  * base64 (обычный/URL-safe), в т.ч. с переносами;
  * JSON с вложенными строками;
  * HTML (в т.ч. с &amp;).

Обновление атомарное: новый конфиг сначала пишется во временный файл и
проверяется `xray run -test`, и только потом заменяет рабочий. При ошибке
старый конфиг остаётся на месте.

Пример:
    python3 scripts/update_xray_config.py \
        --url-file /etc/xray-subscription.url \
        --write /usr/local/etc/xray/config.json \
        --restart
"""

from __future__ import annotations

import argparse
import base64
import grp
import html
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# При запуске как `python scripts/...` корень репозитория не в sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.xray_vless import VlessError, build_xray_config

VLESS_RE = re.compile(r"vless://[^\s\"'<>\\]+", re.IGNORECASE)
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class SubscriptionError(RuntimeError):
    """Не удалось получить или разобрать subscription."""


def _normalize(text: str) -> str:
    return html.unescape(text).replace("\\/", "/")


def _links(text: str) -> list[str]:
    return VLESS_RE.findall(_normalize(text))


def _iter_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item)


def _dedupe(links: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for link in links:
        if link not in seen:
            seen.add(link)
            result.append(link)
    return result


def extract_vless_links(raw: str) -> list[str]:
    """Извлекает vless://-ссылки из plain/base64/JSON/HTML ответа."""
    direct = _links(raw)
    if direct:
        return _dedupe(direct)

    compact = re.sub(r"\s+", "", raw)
    if compact:
        padded = compact + "=" * (-len(compact) % 4)
        try:
            decoded = base64.b64decode(
                padded, altchars=b"-_", validate=False
            ).decode("utf-8", errors="replace")
        except (ValueError, UnicodeDecodeError):
            decoded = ""
        if decoded:
            links = _links(decoded)
            if links:
                return _dedupe(links)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None
    if payload is not None:
        links = []
        for value in _iter_strings(payload):
            links.extend(_links(value))
        if links:
            return _dedupe(links)

    return []


def fetch_subscription(url: str, *, timeout: float = 20.0) -> str:
    """Скачивает subscription как браузер (не логируя URL)."""
    if not url.lower().startswith(("http://", "https://")):
        raise SubscriptionError("Subscription URL должен начинаться с http(s)://")
    request = Request(
        url,
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        },
    )
    try:
        # Схема уже проверена выше — только http/https.
        with urlopen(request, timeout=timeout) as response:  # nosec B310
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except HTTPError as exc:
        raise SubscriptionError(f"HTTP {exc.code} при загрузке subscription") from exc
    except URLError as exc:
        raise SubscriptionError(f"Не удалось загрузить subscription: {exc.reason}") from exc


def read_url(args: argparse.Namespace) -> str:
    if args.url:
        return args.url.strip()
    if args.url_file:
        try:
            return args.url_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SubscriptionError(f"Не удалось прочитать {args.url_file}") from exc
    env = os.getenv("VLESS_SUBSCRIPTION_URL", "").strip()
    if env:
        return env
    raise SubscriptionError(
        "Укажите --url, --url-file или VLESS_SUBSCRIPTION_URL"
    )


def _validate_config(config: dict[str, Any], xray_bin: str) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
        temp_name = handle.name
    try:
        result = subprocess.run(
            [xray_bin, "run", "-test", "-c", temp_name],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode != 0:
            raise SubscriptionError(
                "Xray не принял конфиг: "
                + (result.stderr or result.stdout or "неизвестная ошибка").strip()
            )
    finally:
        Path(temp_name).unlink(missing_ok=True)


def _parse_owner(value: str) -> tuple[int, int]:
    """Преобразует xray:xray / nobody:nogroup в uid/gid."""
    user, _, group = value.partition(":")
    try:
        pw = pwd.getpwnam(user)
    except KeyError as exc:
        raise SubscriptionError(f"Пользователь {user!r} не найден") from exc
    gid = pw.pw_gid
    if group:
        try:
            gid = grp.getgrnam(group).gr_gid
        except KeyError as exc:
            raise SubscriptionError(f"Группа {group!r} не найдена") from exc
    return pw.pw_uid, gid


def _service_owner(service: str) -> tuple[int, int] | None:
    """Берёт User/Group из systemd-сервиса Xray (например, xray:xray)."""
    try:
        result = subprocess.run(
            ["systemctl", "show", "-p", "User", "-p", "Group", service],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    props: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            props[key.strip()] = value.strip()
    user = props.get("User") or "root"
    group = props.get("Group") or ""
    try:
        return _parse_owner(f"{user}:{group}" if group else user)
    except SubscriptionError:
        return None


def _write_atomic(
    path: Path,
    config: dict[str, Any],
    *,
    validate: bool,
    xray_bin: str,
    owner: tuple[int, int] | None = None,
) -> bytes | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.read_bytes() if path.exists() else None
    old_stat = path.stat() if path.exists() else None
    if validate:
        _validate_config(config, xray_bin)

    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    os.close(fd)
    try:
        temp_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temp_path, 0o600)
        # Xray обычно работает от nobody — конфиг должен быть ему доступен.
        if owner is not None:
            os.chown(temp_path, owner[0], owner[1])
        elif old_stat is not None:
            os.chown(temp_path, old_stat.st_uid, old_stat.st_gid)
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return old


def _restart(service: str) -> None:
    result = subprocess.run(
        ["systemctl", "restart", service],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise SubscriptionError(
            f"systemctl restart {service} не удался: "
            + (result.stderr or result.stdout or "неизвестная ошибка").strip()
        )


def update_once(args: argparse.Namespace) -> int:
    url = read_url(args)
    raw = fetch_subscription(url, timeout=args.timeout)
    links = extract_vless_links(raw)
    if not links:
        raise SubscriptionError(
            "В ответе не найдено ни одной vless:// ссылки "
            "(plain/base64/JSON/HTML проверены)"
        )
    if args.index < 0 or args.index >= len(links):
        raise SubscriptionError(
            f"Индекс {args.index} вне диапазона: найдено {len(links)} ссылок"
        )

    config = build_xray_config(links[args.index], socks_port=args.socks_port)
    vnext = config["outbounds"][0]["settings"]["vnext"][0]
    stream = config["outbounds"][0]["streamSettings"]
    summary = (
        f"{vnext['address']}:{vnext['port']} "
        f"{stream.get('network')}/{stream.get('security')}"
    )

    if args.dry_run:
        print(f"dry-run: {summary}")
        return 0

    xray_bin = args.xray or shutil.which("xray")
    if args.validate and not xray_bin:
        raise SubscriptionError("xray не найден; установите Xray или --no-validate")

    owner = _parse_owner(args.owner) if args.owner else _service_owner(args.service)
    old = _write_atomic(
        args.write,
        config,
        validate=args.validate,
        xray_bin=xray_bin or "xray",
        owner=owner,
    )
    print(f"config updated: {args.write} ({summary})")

    if args.restart:
        try:
            _restart(args.service)
        except SubscriptionError:
            if old is not None:
                args.write.write_bytes(old)
                try:
                    _restart(args.service)
                except SubscriptionError:
                    pass
            raise
        print(f"restarted: {args.service}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Обновить Xray config из VLESS subscription URL."
    )
    parser.add_argument("--url", help="subscription URL")
    parser.add_argument("--url-file", type=Path, help="файл с subscription URL")
    parser.add_argument(
        "--write",
        type=Path,
        default=Path("/usr/local/etc/xray/config.json"),
        help="куда писать Xray config",
    )
    parser.add_argument("--socks-port", type=int, default=1080)
    parser.add_argument("--index", type=int, default=0, help="какую ссылку выбрать")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--xray", help="путь к бинарнику xray")
    parser.add_argument(
        "--owner",
        help="владелец config.json, например xray:xray; "
        "по умолчанию определяется из systemd-сервиса Xray",
    )
    parser.add_argument("--service", default="xray", help="имя systemd-сервиса")
    parser.add_argument("--restart", action="store_true", help="перезапустить Xray")
    parser.add_argument(
        "--no-validate", action="store_true", help="не запускать xray run -test"
    )
    parser.add_argument("--dry-run", action="store_true", help="только проверить")
    args = parser.parse_args(argv)
    args.validate = not args.no_validate

    try:
        return update_once(args)
    except (SubscriptionError, VlessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
