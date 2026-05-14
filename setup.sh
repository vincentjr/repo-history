#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 not found on PATH" >&2
  exit 1
fi

# pyproject requires >=3.11
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || {
  echo "error: Python 3.11+ required; found $(python3 --version)" >&2
  exit 1
}

if [ ! -d .venv ]; then
  echo "Creating virtual environment in .venv/ ..."
  python3 -m venv .venv
fi

echo "Installing dependencies ..."
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -e ".[dev]"

echo
echo "Done. Next:"
echo "  ./code-history init                 # write generated/config.toml"
echo "  \$EDITOR generated/config.toml       # set token + repos"
echo "  ./code-history fetch --repo owner/repo --limit 25"
