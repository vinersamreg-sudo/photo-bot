"""Run the privacy-safe Ravuna production watchdog."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time

from app.config import load_settings
from app.watchdog import (
    alert_transport_from_environment,
    apply_auto_heal,
    auto_heal_state_path,
    build_watchdog_report,
    read_only_application_errors,
    render_watchdog,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--format", choices=("human", "json"), default="human")
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--auto-heal", action="store_true")
    return parser


def _build_report(settings, *, online: bool) -> dict[str, object]:
    return build_watchdog_report(
        settings,
        online=online,
        application_errors=read_only_application_errors(settings),
    )


def _restart_service_once() -> bool:
    try:
        result = subprocess.run(
            ["sudo", "-n", "systemctl", "restart", "photo-bot.service"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=45,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()
    online = not args.offline
    report = _build_report(settings, online=online)
    if args.auto_heal:
        report = apply_auto_heal(
            report,
            state_path=auto_heal_state_path(settings),
            restart_service=_restart_service_once,
            refresh_report=lambda: _build_report(settings, online=online),
            sleep=time.sleep,
        )
    transport = alert_transport_from_environment(dict(os.environ))
    report["alert_transport"] = "configured" if transport.configured else "not_configured"
    recovered = (report.get("auto_heal") or {}).get("state") == "RECOVERED"
    if args.notify and (report["exit_code"] or recovered) and transport.configured:
        try:
            transport.send(report)
            report["alert_transport"] = "sent"
        except Exception:
            report["alert_transport"] = "failed"
            report["status"] = "CRITICAL"
            report["exit_code"] = 2
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(render_watchdog(report))
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
