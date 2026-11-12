"""Private mTLS image boundary. The executable supports only a mock upstream."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import ipaddress
import json
import re
import sqlite3
import ssl
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener

from PIL import Image

from app.domain import PolicyRejectedError, ProviderQuotaError, ProviderResult, ProviderTimeoutError, ProviderUnavailableError
from app.image_provider import OpenAIImageProvider, _resolve_output_size

PATH = "/v1/edit"
CLIENT_NAME = ("DNS", "ravuna-core.internal")
MAX_IMAGE = 15 * 1024 * 1024
MAX_BODY = 41 * 1024 * 1024
OP_TTL = 300
FAILURES = {
    "policy": PolicyRejectedError, "quota": ProviderQuotaError,
    "timeout": ProviderTimeoutError, "unavailable": ProviderUnavailableError,
}
PRIVATE_NETWORKS = tuple(map(ipaddress.IPv4Network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))


def _private_address(address: str) -> bool:
    try:
        ip = ipaddress.IPv4Address(address)
        return ip.is_loopback or any(ip in network for network in PRIVATE_NETWORKS)
    except ValueError:
        return False


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _usage(value):
    """Copy only numeric accounting fields, never upstream message text."""
    if not isinstance(value, dict):
        return {}
    return {
        key: _usage(item) if isinstance(item, dict) else item
        for key, item in value.items()
        if key in {"input_tokens", "output_tokens", "total_tokens", "text_tokens", "image_tokens",
                   "cached_tokens", "input_tokens_details", "output_tokens_details"}
        and (isinstance(item, dict) or type(item) is int and item >= 0)
    }


class Gateway:
    """One process, one synchronous worker; only opaque receipts persist."""

    def __init__(self, journal: Path, provider: OpenAIImageProvider):
        self.journal = journal
        self.provider = provider
        self.last = None
        with self._db() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS attempted (id TEXT PRIMARY KEY, expires INTEGER NOT NULL)")

    def expire(self):
        if self.last is not None and time.monotonic() >= self.last[3]:
            self.last = None

    def _db(self):
        # Connection context manager does not close sqlite connections.
        return _Journal(self.journal)

    def edit(self, raw: bytes) -> tuple[int, bytes]:
        self.expire()
        if not 0 < len(raw) <= MAX_BODY:
            return 413, _json({"error": "unavailable"})
        try:
            request = json.loads(raw)
            if not isinstance(request, dict) or set(request) != {"id", "prompt", "images"}:
                raise ValueError
            operation = request["id"]
            if not isinstance(operation, str) or not re.fullmatch(r"[0-9]{10}-[0-9a-f]{32}", operation):
                raise ValueError
            now = int(time.time())
            issued = int(operation[:10])
            if not now - OP_TTL <= issued <= now + 15:
                return 409, _json({"error": "unavailable"})
            prompt = request["prompt"]
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > 32 * 1024:
                raise ValueError
            images = request["images"]
            if not isinstance(images, list) or not 1 <= len(images) <= 2:
                raise ValueError
            data = tuple(base64.b64decode(item, validate=True) for item in images)
            if any(not 0 < len(item) <= MAX_IMAGE for item in data):
                return 413, _json({"error": "unavailable"})
            for item in data:
                with Image.open(io.BytesIO(item)) as image:
                    if image.format not in {"JPEG", "PNG", "WEBP"} or image.width * image.height > 32_000_000:
                        raise ValueError
                    image.verify()
        except (ValueError, TypeError, OSError, RecursionError, Image.DecompressionBombError):
            return 400, _json({"error": "unavailable"})
        digest = hashlib.sha256(raw).digest()
        if self.last is not None and self.last[0] == operation and self.last[1] == digest:
            return self.last[2]
        with self._db() as conn:
            conn.execute("DELETE FROM attempted WHERE expires<?", (now,))
            inserted = conn.execute("INSERT OR IGNORE INTO attempted VALUES(?,?)", (operation, issued + OP_TTL)).rowcount
        if not inserted:
            return 409, _json({"error": "unavailable"})
        self.last = None
        try:
            result = self.provider.edit_bytes(data, prompt)
            if not 0 < len(result.image_bytes) <= MAX_IMAGE:
                raise ValueError
            reply = 200, _json({
                "image": base64.b64encode(result.image_bytes).decode("ascii"),
                "usage": _usage(dict(result.usage)), "retries": result.retries,
                "http_status": result.http_status, "duration_ms": result.provider_duration_ms,
            })
        except Exception as exc:
            error = next((key for key, cls in FAILURES.items() if isinstance(exc, cls)), "unavailable")
            reply = 422, _json({"error": error})
        self.last = operation, digest, reply, time.monotonic() + 90
        return reply


class _Journal:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, timeout=2)

    def __enter__(self):
        return self.conn

    def __exit__(self, *args):
        try:
            return self.conn.__exit__(*args)
        finally:
            self.conn.close()


def serve(gateway: Gateway, address: str, port: int, *, ca: str, cert: str, key: str):
    if not _private_address(address):
        raise ValueError("AI gateway requires a loopback or private tunnel bind address")
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.verify_mode = ssl.CERT_REQUIRED
    tls.load_verify_locations(cafile=ca)
    tls.load_cert_chain(cert, key)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, status, data):
            self.send_response_only(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if CLIENT_NAME not in self.connection.getpeercert().get("subjectAltName", ()):
                return self.reply(403, b"{}")
            if self.path != PATH:
                return self.reply(404, b"{}")
            lengths = self.headers.get_all("Content-Length", [])
            if (self.headers.get("Transfer-Encoding") or len(lengths) != 1 or
                    not re.fullmatch(r"[0-9]{1,9}", lengths[0]) or
                    self.headers.get_content_type() != "application/json"):
                return self.reply(400, b"{}")
            length = int(lengths[0])
            if not 0 < length <= MAX_BODY:
                return self.reply(413, b"{}")
            raw = self.rfile.read(length)
            if len(raw) != length:
                return self.reply(400, b"{}")
            return self.reply(*gateway.edit(raw))

    class Server(HTTPServer):
        def service_actions(self):
            gateway.expire()

        def get_request(self):
            conn, peer = super().get_request()
            try:
                conn.settimeout(5)
                return tls.wrap_socket(conn, server_side=True), peer
            except OSError:
                conn.close()
                raise

        def handle_error(self, *_args):
            # Don't dump exception bodies, request contents or peer identifiers.
            pass

    return Server((address, port), Handler)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class RemoteImageProvider:
    name = "openai"

    def __init__(self, settings):
        url = urlsplit(settings.ai_gateway_url)
        if (url.scheme != "https" or not _private_address(url.hostname or "") or url.username is not None or
                url.password is not None or url.path not in {"", "/"} or url.query or url.fragment):
            raise ValueError("AI gateway URL must be a private HTTPS origin")
        self.url = settings.ai_gateway_url.rstrip("/") + PATH
        self.model = settings.openai_image_model
        self.size = settings.image_edit_size
        self.quality = settings.image_edit_quality
        self.output_format = settings.image_edit_output_format
        self.timeout = settings.generation_timeout_seconds
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_verify_locations(cafile=settings.ai_gateway_ca)
        tls.load_cert_chain(settings.ai_gateway_cert, settings.ai_gateway_key)
        self.opener = build_opener(ProxyHandler({}), _NoRedirect(), HTTPSHandler(context=tls))

    def resolve_size(self, source_path: Path) -> str:
        return _resolve_output_size(self.size, source_path)

    def edit(self, source_path: Path, prompt: str) -> ProviderResult:
        return self.edit_many((source_path,), prompt)

    def edit_many(self, source_paths: tuple[Path, ...], prompt: str) -> ProviderResult:
        if not 1 <= len(source_paths) <= 2:
            raise ValueError("AI gateway accepts one or two images")
        images = []
        for path in source_paths:
            with path.open("rb") as handle:
                data = handle.read(MAX_IMAGE + 1)
            if len(data) > MAX_IMAGE:
                raise ProviderUnavailableError("AI gateway input too large")
            images.append(base64.b64encode(data).decode("ascii"))
        request = Request(self.url, data=_json({
            "id": f"{int(time.time())}-{uuid.uuid4().hex}", "images": images, "prompt": prompt,
        }), headers={"Content-Type": "application/json"})
        deadline = time.monotonic() + self.timeout
        for attempt in range(2):
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                with self.opener.open(request, timeout=remaining) as response:
                    raw = response.read(MAX_BODY + 1)
                if len(raw) > MAX_BODY:
                    raise ValueError
                value = json.loads(raw)
                result = base64.b64decode(value["image"], validate=True)
                if not 0 < len(result) <= MAX_IMAGE:
                    raise ValueError
                return ProviderResult(
                    image_bytes=result, usage=_usage(value.get("usage")),
                    retries=int(value.get("retries", 0)),
                    provider_name=self.name, provider_model=self.model, image_model=self.model,
                    http_status=value.get("http_status"), provider_duration_ms=value.get("duration_ms"),
                )
            except HTTPError as exc:
                with exc:
                    try:
                        code = json.loads(exc.read(1024)).get("error") if exc.code == 422 else None
                    except (ValueError, AttributeError):
                        code = None
                raise FAILURES.get(code if isinstance(code, str) else "", ProviderUnavailableError)("AI gateway request rejected") from None
            except (OSError, TimeoutError):
                # Reuse the identical operation and body. Server-side receipts
                # prevent re-execution even if the first response was lost.
                if attempt == 0 and time.monotonic() < deadline:
                    continue
                raise ProviderTimeoutError("AI gateway outcome unavailable") from None
            except (ValueError, KeyError, TypeError, AttributeError):
                raise ProviderUnavailableError("Invalid AI gateway response") from None
        raise AssertionError("unreachable")


def mock_openai():
    """Synthetic PNG fixture through the real OpenAIImageProvider interface."""
    image = io.BytesIO()
    Image.new("RGB", (32, 32), (35, 89, 153)).save(image, "PNG")
    b64 = base64.b64encode(image.getvalue()).decode("ascii")
    return SimpleNamespace(images=SimpleNamespace(
        edit=lambda **_kwargs: SimpleNamespace(data=[SimpleNamespace(b64_json=b64)])
    ))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock", action="store_true", required=True)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9443)
    parser.add_argument("--state", type=Path, required=True)
    for option in ("ca", "cert", "key", "model"):
        parser.add_argument(f"--{option}", required=True)
    args = parser.parse_args()
    provider = OpenAIImageProvider(mock_openai(), args.model, quality="medium")
    with serve(Gateway(args.state, provider), args.bind, args.port, ca=args.ca, cert=args.cert, key=args.key) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
