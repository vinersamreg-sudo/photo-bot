from __future__ import annotations

import hashlib
import html
import ipaddress
import mimetypes
import re
import socket
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, unquote, urlsplit

from app.database import ReadOnlyDatabase


SAMARA = timezone(timedelta(hours=4), name="SAMT")
ATTEMPT_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
SAFE_ERROR_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
MEDIA_ROLES = {"source", "source-2", "preview"}
IMAGE_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def mask_client_id(user_id: str) -> str:
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:10]
    return f"client-{digest}"


def safe_error_category(value: Any) -> str:
    candidate = str(value or "unknown_error").strip()
    return candidate if SAFE_ERROR_RE.fullmatch(candidate) else "unknown_error"


def samara_datetime(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "—"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=SAMARA)
        return parsed.astimezone(SAMARA).strftime("%d.%m.%Y %H:%M:%S")
    except ValueError:
        return "—"


def format_duration(value: Any) -> str:
    if value is None:
        return "—"
    try:
        milliseconds = max(0, int(value))
    except (TypeError, ValueError):
        return "—"
    if milliseconds < 1000:
        return f"{milliseconds} ms"
    return f"{milliseconds / 1000:.1f} s"


def public_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    if status == "succeeded":
        return "SUCCESS"
    if status in {"pending", "processing"}:
        return "PROCESSING"
    return "ERROR"


def require_loopback(host: str) -> None:
    candidate = host.strip()
    if not candidate:
        raise ValueError("Admin journal host must be loopback")
    try:
        addresses = {ipaddress.ip_address(candidate)}
    except ValueError:
        try:
            addresses = {
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(candidate, None, type=socket.SOCK_STREAM)
            }
        except (OSError, ValueError) as exc:
            raise ValueError("Admin journal host must resolve to loopback") from exc
    if not addresses or any(not address.is_loopback for address in addresses):
        raise ValueError("Admin journal may bind only to loopback")


@dataclass(frozen=True)
class MediaFile:
    path: Path
    content_type: str


class AdminJournal:
    """Read-only generation journal backed by the existing DB and private media."""

    def __init__(self, database_path: Path, private_users_root: Path) -> None:
        self.database = ReadOnlyDatabase(database_path)
        self.private_users_root = private_users_root

    @staticmethod
    def _query() -> str:
        return """
            SELECT a.id,a.user_id,a.prompt,a.status,a.started_at,a.completed_at,
                   COALESCE(NULLIF(a.processing_provider,''),a.provider) AS display_provider,
                   COALESCE(NULLIF(a.processing_provider_model,''),a.model) AS display_model,
                   COALESCE(a.provider_duration_ms,a.duration_ms) AS display_duration_ms,
                   a.provider_http_status,a.error_type,a.correction,
                   CASE WHEN a.correction=1
                        THEN source_version.preview_watermarked_path
                        ELSE a.source_path END AS display_source_path,
                   CASE WHEN a.correction=1 THEN NULL ELSE a.secondary_source_path END
                        AS display_secondary_source_path,
                   COALESCE(result_version.preview_watermarked_path,a.demo_result_path)
                        AS display_preview_path
              FROM generation_attempts a
              LEFT JOIN gallery_versions result_version ON result_version.attempt_id=a.id
              LEFT JOIN gallery_versions source_version ON source_version.id=a.source_version_id
        """

    def latest(self, limit: int = 100) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 100))
        with self.database.read() as connection:
            rows = connection.execute(
                self._query() + " ORDER BY a.started_at DESC,a.id DESC LIMIT ?",
                (bounded,),
            ).fetchall()
        return [self._public_attempt(dict(row)) for row in rows]

    def get(self, attempt_id: str) -> Optional[dict[str, Any]]:
        if not ATTEMPT_ID_RE.fullmatch(attempt_id):
            return None
        with self.database.read() as connection:
            row = connection.execute(
                self._query() + " WHERE a.id=? LIMIT 1",
                (attempt_id,),
            ).fetchone()
        return self._public_attempt(dict(row)) if row is not None else None

    def _public_attempt(self, row: dict[str, Any]) -> dict[str, Any]:
        status = public_status(row.get("status"))
        attempt_id = str(row["id"])
        source = self._resolve_private_image(row.get("display_source_path"))
        secondary = self._resolve_private_image(row.get("display_secondary_source_path"))
        preview = self._resolve_private_image(row.get("display_preview_path"))
        return {
            "id": attempt_id,
            "reference": attempt_id[:8],
            "client": mask_client_id(str(row.get("user_id") or "")),
            "prompt": str(row.get("prompt") or ""),
            "status": status,
            "started_at": samara_datetime(row.get("started_at")),
            "completed_at": samara_datetime(row.get("completed_at")),
            "provider": str(row.get("display_provider") or "unknown"),
            "model": str(row.get("display_model") or "unknown"),
            "duration": format_duration(row.get("display_duration_ms")),
            "http_status": (
                str(int(row["provider_http_status"]))
                if row.get("provider_http_status") is not None
                else "—"
            ),
            "error_category": (
                safe_error_category(row.get("error_type")) if status == "ERROR" else None
            ),
            "source_available": source is not None,
            "secondary_source_available": secondary is not None,
            "preview_available": status == "SUCCESS" and preview is not None,
            "source_label": (
                "Предыдущая версия (watermarked)"
                if int(row.get("correction") or 0)
                else "Исходник"
            ),
        }

    def media(self, attempt_id: str, role: str) -> Optional[MediaFile]:
        if role not in MEDIA_ROLES or not ATTEMPT_ID_RE.fullmatch(attempt_id):
            return None
        with self.database.read() as connection:
            row = connection.execute(
                self._query() + " WHERE a.id=? LIMIT 1",
                (attempt_id,),
            ).fetchone()
        if row is None:
            return None
        values = dict(row)
        selected = {
            "source": values.get("display_source_path"),
            "source-2": values.get("display_secondary_source_path"),
            "preview": (
                values.get("display_preview_path")
                if public_status(values.get("status")) == "SUCCESS"
                else None
            ),
        }[role]
        return self._resolve_private_image(selected)

    def _resolve_private_image(self, value: Any) -> Optional[MediaFile]:
        raw = str(value or "").strip()
        if not raw:
            return None
        path = Path(raw)
        try:
            root = self.private_users_root.resolve(strict=True)
            if self.private_users_root.is_symlink():
                return None
            absolute = path if path.is_absolute() else self.private_users_root.parent.parent / path
            relative = absolute.absolute().relative_to(self.private_users_root.absolute())
            current = self.private_users_root.absolute()
            for part in relative.parts:
                current = current / part
                if current.is_symlink():
                    return None
            resolved = absolute.resolve(strict=True)
        except (FileNotFoundError, OSError, RuntimeError, ValueError):
            return None
        if root not in resolved.parents or not resolved.is_file():
            return None
        content_type = IMAGE_MIME_TYPES.get(resolved.suffix.lower())
        if content_type is None:
            guessed, _ = mimetypes.guess_type(resolved.name)
            if guessed not in IMAGE_MIME_TYPES.values():
                return None
            content_type = guessed
        return MediaFile(resolved, content_type)


BASE_CSS = """
:root{color-scheme:dark;--bg:#0b1020;--card:#151d33;--muted:#98a4bd;--line:#26314c;
--ok:#58d68d;--bad:#ff7f87;--busy:#f8c96b;--accent:#8ca6ff}
*{box-sizing:border-box}body{margin:0;background:linear-gradient(145deg,#0b1020,#10172a 55%,#0b1020);
color:#f7f9ff;font:15px/1.45 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:inherit;text-decoration:none}.shell{width:min(1180px,calc(100% - 28px));margin:0 auto;padding:26px 0 50px}
.top{display:flex;justify-content:space-between;align-items:end;gap:18px;margin-bottom:22px}.eyebrow{color:var(--accent);
font-size:12px;font-weight:800;letter-spacing:.14em;text-transform:uppercase}.top h1{font-size:clamp(25px,4vw,38px);margin:5px 0 2px}
.muted{color:var(--muted)}.count{padding:7px 11px;border:1px solid var(--line);border-radius:999px;white-space:nowrap}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.card{background:rgba(21,29,51,.92);
border:1px solid var(--line);border-radius:18px;overflow:hidden;box-shadow:0 16px 40px rgba(0,0,0,.14)}
.card:hover{border-color:#41517b}.thumbs{height:190px;display:grid;grid-auto-flow:column;grid-auto-columns:1fr;background:#0a0f1c}
.thumbs img{width:100%;height:190px;object-fit:cover;min-width:0}.empty{height:190px;display:grid;place-items:center;color:#68728a;background:#0e1424}
.body{padding:16px}.row{display:flex;align-items:center;justify-content:space-between;gap:12px}.badge{font-size:11px;font-weight:850;
letter-spacing:.08em;padding:5px 8px;border-radius:999px}.success{color:var(--ok);background:#133928}.error{color:var(--bad);background:#422129}
.processing{color:var(--busy);background:#3d321a}.prompt{white-space:pre-wrap;word-break:break-word;margin:14px 0 12px;font:600 15px/1.45 inherit}
.meta{display:flex;flex-wrap:wrap;gap:7px;color:var(--muted);font-size:12px}.meta span{background:#10172a;border:1px solid #222e49;padding:5px 7px;border-radius:8px}
.back{display:inline-flex;margin-bottom:17px;color:var(--muted)}.detail{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(290px,.75fr);gap:18px}
.media-panel,.info-panel{background:rgba(21,29,51,.94);border:1px solid var(--line);border-radius:20px;padding:17px}.media-stack{display:grid;gap:13px}
.media-box{background:#0a0f1c;border-radius:14px;overflow:hidden;border:1px solid var(--line)}.media-box img{display:block;width:100%;max-height:560px;object-fit:contain}
.media-label{padding:8px 11px;color:var(--muted);font-size:12px}.info-panel h2{margin:0 0 8px}.facts{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin:18px 0}
.fact{padding:10px;border:1px solid var(--line);border-radius:12px}.fact b{display:block;font-size:12px;color:var(--muted);margin-bottom:3px}
.prompt-box{white-space:pre-wrap;word-break:break-word;background:#0e1424;border:1px solid var(--line);border-radius:14px;padding:14px}
.notice{padding:14px;border:1px dashed var(--line);border-radius:14px;color:var(--muted)}
@media(max-width:760px){.shell{width:min(100% - 18px,1180px);padding-top:17px}.top{align-items:start;flex-direction:column}.grid,.detail{grid-template-columns:1fr}
.thumbs,.thumbs img,.empty{height:170px}.info-panel{order:-1}.facts{grid-template-columns:1fr 1fr}.media-box img{max-height:470px}}
"""


def _page(title: str, body: str) -> bytes:
    return (
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)} · Ravuna</title><style>{BASE_CSS}</style></head>"
        f"<body>{body}</body></html>"
    ).encode("utf-8")


def _badge(status: str) -> str:
    css = status.lower()
    return f"<span class='badge {css}'>{html.escape(status)}</span>"


def render_index(attempts: list[dict[str, Any]]) -> bytes:
    cards: list[str] = []
    for attempt in attempts:
        media: list[str] = []
        encoded = quote(str(attempt["id"]), safe="")
        if attempt["source_available"]:
            media.append(f"<img loading='lazy' alt='Исходник' src='/media/{encoded}/source'>")
        if attempt["preview_available"]:
            media.append(f"<img loading='lazy' alt='Watermarked preview' src='/media/{encoded}/preview'>")
        thumbnails = "".join(media) or "<div class='empty'>Файлы уже удалены по retention</div>"
        error = (
            f"<span>error: {html.escape(str(attempt['error_category']))}</span>"
            if attempt["error_category"] else ""
        )
        cards.append(
            f"<a class='card' href='/attempt/{encoded}'><div class='thumbs'>{thumbnails}</div>"
            f"<div class='body'><div class='row'>{_badge(str(attempt['status']))}"
            f"<span class='muted'>#{html.escape(str(attempt['reference']))}</span></div>"
            f"<div class='prompt'>{html.escape(str(attempt['prompt']))}</div><div class='meta'>"
            f"<span>{html.escape(str(attempt['started_at']))} Самара</span>"
            f"<span>{html.escape(str(attempt['client']))}</span>"
            f"<span>{html.escape(str(attempt['provider']))} / {html.escape(str(attempt['model']))}</span>"
            f"<span>{html.escape(str(attempt['duration']))}</span>{error}</div></div></a>"
        )
    content = "".join(cards) or "<div class='notice'>Обработок пока нет.</div>"
    return _page(
        "Журнал обработок",
        "<main class='shell'><header class='top'><div><div class='eyebrow'>Ravuna · private admin</div>"
        "<h1>Журнал обработок</h1><div class='muted'>Последние запросы, новые сверху · время Самары</div></div>"
        f"<div class='count'>{len(attempts)} из 100</div></header><section class='grid'>{content}</section></main>",
    )


def render_detail(attempt: dict[str, Any]) -> bytes:
    encoded = quote(str(attempt["id"]), safe="")
    media: list[str] = []
    if attempt["source_available"]:
        media.append(
            f"<div class='media-box'><div class='media-label'>{html.escape(str(attempt['source_label']))}</div>"
            f"<img alt='Source' src='/media/{encoded}/source'></div>"
        )
    if attempt["secondary_source_available"]:
        media.append(
            f"<div class='media-box'><div class='media-label'>Второй исходник</div>"
            f"<img alt='Second source' src='/media/{encoded}/source-2'></div>"
        )
    if attempt["preview_available"]:
        media.append(
            f"<div class='media-box'><div class='media-label'>Результат с watermark</div>"
            f"<img alt='Watermarked result' src='/media/{encoded}/preview'></div>"
        )
    media_html = "".join(media) or "<div class='notice'>Исторические media уже недоступны.</div>"
    error = (
        f"<div class='fact'><b>Категория ошибки</b>{html.escape(str(attempt['error_category']))}</div>"
        if attempt["error_category"] else ""
    )
    body = (
        "<main class='shell'><a class='back' href='/'>← Последние обработки</a><section class='detail'>"
        f"<div class='media-panel'><div class='media-stack'>{media_html}</div></div>"
        f"<aside class='info-panel'><div class='row'>{_badge(str(attempt['status']))}"
        f"<span class='muted'>#{html.escape(str(attempt['reference']))}</span></div>"
        "<div class='facts'>"
        f"<div class='fact'><b>Клиент</b>{html.escape(str(attempt['client']))}</div>"
        f"<div class='fact'><b>Начало</b>{html.escape(str(attempt['started_at']))} Самара</div>"
        f"<div class='fact'><b>Provider / model</b>{html.escape(str(attempt['provider']))}<br>{html.escape(str(attempt['model']))}</div>"
        f"<div class='fact'><b>Duration</b>{html.escape(str(attempt['duration']))}</div>"
        f"<div class='fact'><b>HTTP</b>{html.escape(str(attempt['http_status']))}</div>{error}</div>"
        "<h2>Запрос пользователя</h2>"
        f"<div class='prompt-box'>{html.escape(str(attempt['prompt']))}</div>"
        "<p class='muted'>Provider prompt, request ID, usage JSON, payment data и originals не отображаются.</p>"
        "</aside></section></main>"
    )
    return _page("Карточка обработки", body)


class AdminJournalServer:
    def __init__(self, journal: AdminJournal, host: str = "127.0.0.1", port: int = 8092) -> None:
        require_loopback(host)
        self.journal = journal
        self.host = host
        self.port = int(port)
        if not 0 <= self.port <= 65535:
            raise ValueError("Admin journal port is outside the valid range")
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def build(self) -> ThreadingHTTPServer:
        journal = self.journal

        class Handler(BaseHTTPRequestHandler):
            server_version = "RavunaAdmin/1"

            def _headers(self, status: HTTPStatus, content_type: str, length: int = 0) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(length))
                self.send_header("Cache-Control", "no-store, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; img-src 'self'; style-src 'unsafe-inline'; "
                    "base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
                )
                self.end_headers()

            def _bytes(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
                self._headers(status, content_type, len(body))
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _not_found(self) -> None:
                self._bytes(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"Not found")

            def _do_get(self) -> None:
                route = urlsplit(self.path).path
                if route == "/":
                    self._bytes(
                        HTTPStatus.OK,
                        "text/html; charset=utf-8",
                        render_index(journal.latest(100)),
                    )
                    return
                match = re.fullmatch(r"/attempt/([^/]+)", route)
                if match:
                    attempt = journal.get(unquote(match.group(1)))
                    if attempt is None:
                        self._not_found()
                    else:
                        self._bytes(
                            HTTPStatus.OK,
                            "text/html; charset=utf-8",
                            render_detail(attempt),
                        )
                    return
                match = re.fullmatch(r"/media/([^/]+)/(source|source-2|preview)", route)
                if match:
                    media = journal.media(unquote(match.group(1)), match.group(2))
                    if media is None:
                        self._not_found()
                        return
                    try:
                        body = media.path.read_bytes()
                    except OSError:
                        self._not_found()
                        return
                    self._bytes(HTTPStatus.OK, media.content_type, body)
                    return
                self._not_found()

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                try:
                    self._do_get()
                except sqlite3.Error:
                    self._bytes(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "text/plain; charset=utf-8",
                        b"Journal data is temporarily unavailable",
                    )

            def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                self.do_GET()

            def _method_not_allowed(self) -> None:
                self._bytes(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "text/plain; charset=utf-8",
                    b"Read-only admin",
                )

            do_POST = _method_not_allowed
            do_PUT = _method_not_allowed
            do_PATCH = _method_not_allowed
            do_DELETE = _method_not_allowed

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        server = ThreadingHTTPServer((self.host, self.port), Handler)
        server.daemon_threads = True
        return server

    def start(self) -> None:
        if self._server is not None:
            return
        self._server = self.build()
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="ravuna-admin-journal",
            daemon=True,
        )
        self._thread.start()

    @property
    def address(self) -> tuple[str, int]:
        if self._server is None:
            raise RuntimeError("Admin journal server is not running")
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None

    def serve_forever(self) -> None:
        server = self.build()
        self._server = server
        try:
            server.serve_forever()
        finally:
            server.server_close()
            self._server = None
