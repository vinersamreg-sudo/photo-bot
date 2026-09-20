"""Fail-closed local/public webhook probes without exposing the secret URL."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import dotenv_values


def request_status(url: str, *, method: str, body: bytes = b"") -> int:
    request = urllib.request.Request(url, data=body if method == "POST" else None, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: probe_ravuna_avito.py ENV_FILE")
    values = dotenv_values(Path(sys.argv[1]))
    secret = str(values.get("AVITO_WEBHOOK_SECRET") or "")
    if len(secret) != 43:
        raise SystemExit("invalid private webhook secret")
    path = f"/integrations/avito/{secret}/messages"
    wrong = "/integrations/avito/BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB/messages"
    checks = {
        "local_malformed": request_status("http://127.0.0.1:8093" + path, method="POST", body=b"{}"),
        "public_malformed": request_status("https://ravuna.ru" + path, method="POST", body=b"{}"),
        "public_method": request_status("https://ravuna.ru" + path, method="GET"),
        "wrong_secret": request_status("https://ravuna.ru" + wrong, method="POST", body=b"{}"),
        "oversized": request_status(
            "https://ravuna.ru" + path, method="POST", body=b"x" * (129 * 1024)
        ),
    }
    expected = {
        "local_malformed": 400,
        "public_malformed": 400,
        "public_method": 405,
        "wrong_secret": 404,
        "oversized": 413,
    }
    if checks != expected:
        raise SystemExit("Avito webhook probes failed: " + json.dumps(checks, sort_keys=True))
    print("WEBHOOK_PROBES=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
