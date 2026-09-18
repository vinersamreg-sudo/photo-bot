"""Generate privacy-safe Markdown and JSON commercial audits from a SQLite snapshot."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.commercial_intelligence import (
    AuditWindow,
    CommercialIntelligence,
    parse_boundary,
    render_json,
    render_markdown,
)


def _comparison(value: str) -> tuple[str, str, str]:
    try:
        label, boundaries = value.split("=", 1)
        start, end = boundaries.split(",", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected LABEL=FROM,TO") from exc
    if not label.strip() or not start.strip() or not end.strip():
        raise argparse.ArgumentTypeError("expected non-empty LABEL=FROM,TO")
    return label.strip(), start.strip(), end.strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--from", dest="start", required=True, help="inclusive ISO date/datetime")
    parser.add_argument("--to", dest="end", required=True, help="exclusive ISO date/datetime")
    parser.add_argument("--timezone", default="Europe/Samara")
    parser.add_argument("--label", default="current")
    parser.add_argument(
        "--compare",
        action="append",
        default=[],
        type=_comparison,
        metavar="LABEL=FROM,TO",
        help="additional independent comparison window; may be repeated",
    )
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser


def _window(label: str, start: str, end: str, timezone_name: str) -> AuditWindow:
    from_time = parse_boundary(start, timezone_name)
    to_time = parse_boundary(end, timezone_name)
    if from_time >= to_time:
        raise ValueError(f"window {label!r}: --from must be before --to")
    return AuditWindow(label=label, start=from_time, end=to_time, timezone_name=timezone_name)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    if not args.database.is_file():
        build_parser().error(f"database does not exist: {args.database}")
    try:
        windows = [_window(args.label, args.start, args.end, args.timezone)]
        windows.extend(
            _window(label, start, end, args.timezone)
            for label, start, end in args.compare
        )
        report = CommercialIntelligence(args.database).calculate(windows)
    except (ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    markdown = render_markdown(report)
    json_text = render_json(report)
    if args.markdown_out:
        args.markdown_out.write_text(markdown, encoding="utf-8", newline="\n")
    if args.json_out:
        args.json_out.write_text(json_text, encoding="utf-8", newline="\n")
    sys.stdout.write(markdown if args.format == "markdown" else json_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
