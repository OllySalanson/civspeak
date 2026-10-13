#!/bin/sh
# One-time setup: a private Python environment in .venv with civspeak and its voice (Kokoro, Whisper)
# installed into it, the speech recognition model in the Hugging Face cache, and the repo guard
# enabled as a pre-commit hook.
set -eu
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
  # Ubuntu's python3 may lack ensurepip; fall back to the user's pip in that case.
  python3 -m venv .venv 2>/dev/null || { rm -rf .venv; python3 -m venv --without-pip .venv; }
fi
if .venv/bin/python -m pip --version >/dev/null 2>&1; then
  .venv/bin/python -m pip install -q -e '.[dev,voice]'
else
  python3 -m pip --python .venv/bin/python install -q -e '.[dev,voice]'
fi
git config core.hooksPath .githooks

# Whisper base.en (about 150 MB) into ~/.cache/huggingface, once; civspeak never downloads at run time.
nice -n 10 .venv/bin/python -c "from faster_whisper import WhisperModel as W; W('base.en', device='cpu', compute_type='int8')" \
  || echo "Could not fetch the Whisper model; the microphone will stay off until this script runs again." >&2

# George's voice: the Kokoro model shared with the pixel-explainer narrator.
kokoro="${CIVSPEAK_KOKORO_DIR:-$HOME/.local/share/pixel-explainer}"
if [ ! -f "$kokoro/kokoro-v1.0.onnx" ] || [ ! -f "$kokoro/voices-v1.0.bin" ]; then
  echo "Kokoro voice files not found in $kokoro; answers will be shown but not spoken." >&2
fi

echo "Ready. Try:  .venv/bin/civspeak ask \"where can I build a campus\""
