#!/bin/sh
# One-time setup: a private Python environment in .venv with civspeak installed into it,
# and the repo guard enabled as a pre-commit hook.
set -eu
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
  # Ubuntu's python3 may lack ensurepip; fall back to the user's pip in that case.
  python3 -m venv .venv 2>/dev/null || { rm -rf .venv; python3 -m venv --without-pip .venv; }
fi
if .venv/bin/python -m pip --version >/dev/null 2>&1; then
  .venv/bin/python -m pip install -q -e '.[dev]'
else
  python3 -m pip --python .venv/bin/python install -q -e '.[dev]'
fi
git config core.hooksPath .githooks

echo "Ready. Try:  .venv/bin/civspeak ask \"where can I build a campus\""
