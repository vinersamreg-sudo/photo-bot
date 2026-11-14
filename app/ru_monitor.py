"""Privacy-safe health and authenticated external alerting for the RU units."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from app.ai_gateway import _NoRedirect
from app.config import load_settings
from app.main import health_errors
from app.max_transport import MaxApiClient, MaxTransportError


def check_health(settings, expected_url):
    if shutil.disk_usage(settings.data_dir).free < 16 * 1024**3 or health_errors(settings):
        return False
    if settings.max_transport_mode != "webhook" or not expected_url:
        return False
    client = MaxApiClient(settings.max_bot_token, settings.max_api_base_url,
                          timeout_seconds=10, ca_bundle=settings.max_ca_bundle_path)
    try:
        response = client._request("GET", "/subscriptions", stage="webhook_readiness")
        return any(isinstance(item, dict) and item.get("url") == expected_url
                   for item in response.get("subscriptions", []))
    finally:
        client.close()


def send_alert(credential, unit):
    url = Path(credential).read_text(encoding="utf-8").strip()
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.fragment:
        return False
    body = json.dumps({"service": "ravuna-ru", "unit": unit, "status": "failed"}).encode()
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    with opener.open(Request(url, data=body, headers={"Content-Type": "application/json"}), timeout=10) as response:
        return 200 <= response.status < 300


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("health", "alert"))
    parser.add_argument("--expected-url")
    parser.add_argument("--credential")
    parser.add_argument("--unit")
    args = parser.parse_args()
    try:
        passed = (check_health(load_settings(), args.expected_url) if args.command == "health"
                  else send_alert(args.credential, args.unit))
    except (OSError, ValueError, TypeError, MaxTransportError):
        passed = False
    print("RAVUNA_RU_" + args.command.upper() + "=" + ("PASS" if passed else "FAIL"))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
