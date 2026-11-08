#!/usr/bin/env bash
# Fails when files, file names or a commit message contain a term from your private
# denylist: names that must never appear in this public repo, such as a former
# employer's products, internal hostnames or colleagues' names.
#
# The list lives outside the repo, in ~/.config/actio/denylist.txt (override with
# ACTIO_DENYLIST): one term per line, matched case-insensitively anywhere in a line.
# Blank lines and lines starting with # are ignored. Without the file the check is
# skipped, so CI and other contributors are unaffected.
#
# Usage: check-denylist.sh FILE...
# pre-commit passes the staged files, or the commit-message file at the commit-msg stage.
set -euo pipefail

list="${ACTIO_DENYLIST:-$HOME/.config/actio/denylist.txt}"
[ -f "$list" ] || exit 0
[ "$#" -gt 0 ] || exit 0

# Trim each term; drop comments and blank lines (an empty pattern would match everything).
terms="$(sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "$list" | grep -v -e '^#' -e '^$' || true)"
[ -n "$terms" ] || exit 0

found=0
# grep treats each line of a multi-line -e pattern as its own pattern.
if grep -HnIiF -e "$terms" -- "$@"; then found=1; fi            # file contents (text only)
if printf '%s\n' "$@" | grep -iF -e "$terms"; then found=1; fi  # file names

if [ "$found" -eq 1 ]; then
  echo "denylist: the lines above contain a private term from $list" >&2
fi
exit "$found"
