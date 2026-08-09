"""Run the privacy-safe Ravuna production watchdog."""

from __future__ import annotations

import argparse
import json
import os

from app.config import load_settings
from app.watchdog import (
    alert_transport_from_environment,
    build_watchdog_report,
    read_only_application_errors,
    render_watchdog,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--format", choices=("human", "json"), default="human")
    parser.add_argument("--notify", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()
    report = build_watchdog_report(
        settings,
        online=not args.offline,
        application_errors=read_only_application_errors(settings),
    )
    transport = alert_transport_from_environment(dict(os.environ))
    report["alert_transport"] = "configured" if transport.configured else "not_configured"
    if args.notify and report["exit_code"] and transport.configured:
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
