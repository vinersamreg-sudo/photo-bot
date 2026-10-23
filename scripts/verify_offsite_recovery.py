"""Hash one encrypted recovery directly inside the downloaded GitHub artifact ZIP."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import zipfile


def verify(archive, name, sha256, expires_at, now=None):
    now = now or datetime.now(timezone.utc)
    expires = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
    if not expires.tzinfo or expires < now + timedelta(days=7):
        raise ValueError('Insufficient off-site lifetime')
    with zipfile.ZipFile(archive) as zipped:
        entries = [entry for entry in zipped.infolist() if entry.filename == name]
        if len(entries) != 1 or entries[0].is_dir():
            raise ValueError('Missing or duplicate recovery')
        digest = hashlib.sha256()
        size = 0
        with zipped.open(entries[0]) as source:
            for block in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(block)
                size += len(block)
        if digest.hexdigest() != sha256 or not size:
            raise ValueError('Off-site hash mismatch')
    return {'status': 'PASS', 'provider': 'github-actions', 'backup_name': name,
            'sha256': sha256, 'size_bytes': size, 'checked_at': now.isoformat(),
            'expires_at': expires.isoformat()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ('archive', 'name', 'sha256', 'expires-at'):
        parser.add_argument('--' + argument, required=True)
    args = parser.parse_args()
    try:
        result = verify(args.archive, args.name, args.sha256, args.expires_at)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        print(json.dumps({'status': 'BLOCKED', 'reason': 'offsite_download_verification_failed'}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
