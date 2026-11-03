#!/usr/bin/env bash
# Install cperr: writes a tiny launcher into a directory on PATH that runs
# bin/cperr with a Python >= 3.11 (the interpreter found at install time).
# Usage: ./install.sh [--prefix DIR]      (default DIR: ~/.local/bin)
#        ./install.sh --uninstall [--prefix DIR]
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="$HOME/.local/bin"
UNINSTALL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix) TARGET_DIR="${2:?--prefix needs a directory}"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) sed -n '2,5p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

LINK="$TARGET_DIR/cperr"

if [[ $UNINSTALL -eq 1 ]]; then
  if [[ -f "$LINK" ]]; then rm "$LINK"; echo "Removed $LINK"; else echo "Nothing to remove at $LINK"; fi
  exit 0
fi

# Find a Python >= 3.11 and pin the shebang-independent check.
PY=""
for c in python3 python3.13 python3.12 python3.11; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [[ -z "$PY" ]]; then
  echo "error: Python 3.11+ not found. Try: brew install python@3.12" >&2
  exit 1
fi

PY_ABS="$(command -v "$PY")"
chmod +x "$SRC_DIR/bin/cperr"
mkdir -p "$TARGET_DIR"
rm -f "$LINK"
printf '#!/bin/sh\nexec "%s" "%s/bin/cperr" "$@"\n' "$PY_ABS" "$SRC_DIR" > "$LINK"
chmod +x "$LINK"
echo "Installed $LINK (python: $PY_ABS, source: $SRC_DIR)"

case ":$PATH:" in
  *":$TARGET_DIR:"*) ;;
  *)
    LINE="export PATH=\"$TARGET_DIR:\$PATH\""
    if ! grep -qsF "$LINE" "$HOME/.zshrc"; then
      printf '\n# added by cperr install.sh\n%s\n' "$LINE" >> "$HOME/.zshrc"
      echo "Added $TARGET_DIR to PATH in ~/.zshrc - run: source ~/.zshrc"
    fi
    ;;
esac
echo "Done. Try: cperr --help"
