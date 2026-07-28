#!/usr/bin/env bash
set -euo pipefail

base=/opt/ravuna-site
current_target="$(readlink -f "$base/current")"
previous_target="$(readlink -f "$base/previous")"

case "$current_target" in "$base"/releases/*) ;; *) echo "Unsafe current target" >&2; exit 1 ;; esac
case "$previous_target" in "$base"/releases/*) ;; *) echo "Unsafe previous target" >&2; exit 1 ;; esac
test -f "$previous_target/index.html"
test -f "$previous_target/version.txt"

ln -sfn "$previous_target" "$base/current.tmp"
mv -Tf "$base/current.tmp" "$base/current"
ln -sfn "$current_target" "$base/previous.tmp"
mv -Tf "$base/previous.tmp" "$base/previous"
basename "$previous_target" > "$base/deployed_commit.txt"

curl --fail --silent --show-error --resolve ravuna.ru:443:127.0.0.1 https://ravuna.ru/ >/dev/null
echo "Rolled back to $(basename "$previous_target")"
