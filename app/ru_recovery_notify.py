"""Deliver a bounded non-secret recovery outcome to an independent receiver."""

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def notify(state, unit, credentials, *, opener=None):
    if state not in {"ok", "fail"} or not re.fullmatch(
        r"ravuna-ru-(?:db-)?(?:recovery(?:-watchdog)?|watchdog)\.service", unit
    ):
        raise ValueError("Unexpected recovery event")
    target = (credentials / "notify.url").read_text(encoding="utf-8").strip()
    bearer = (credentials / "notify.token").read_text(encoding="utf-8").strip()
    url = urlsplit(target)
    if (url.scheme != "https" or not url.hostname or url.username is not None or url.fragment
            or url.query or not re.fullmatch(r"[A-Za-z0-9._~+/=-]{16,512}", bearer)):
        raise ValueError("Invalid notification credentials")
    payload = json.dumps({"state": state, "unit": unit}, separators=(",", ":")).encode()
    request = Request(target, data=payload, headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + bearer}, method="POST")
    with (opener or build_opener(NoRedirect())).open(request, timeout=8) as response:
        if not 200 <= response.status < 300:
            raise OSError("Notification not accepted")


def main():
    try:
        state, unit, credentials = sys.argv[1:]
        notify(state, unit, Path(credentials))
    except (ValueError, OSError, RuntimeError):
        # Do not print exception messages, which can contain a URL or header.
        print("recovery_notification=FAIL")
        return 1
    print("recovery_notification=DELIVERED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
