from __future__ import annotations

import argparse
from pathlib import Path

from app.admin_journal import AdminJournal, AdminJournalServer


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the loopback-only Ravuna admin journal")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8092)
    args = parser.parse_args()

    root = args.root.resolve()
    journal = AdminJournal(
        root / "data" / "photo_bot.sqlite3",
        root / "data" / "users",
    )
    server = AdminJournalServer(journal, args.host, args.port)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
