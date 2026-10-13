#!/usr/bin/env python3
"""Ask a running civspeak questions out loud, without a person: Kokoro says each question in a
voice other than George's, and the recording goes through the same steps as the mic button
(/api/listen, then /api/ask with speech on), timing each step.

    .venv/bin/civspeak serve &                       # in another terminal
    .venv/bin/python tools/voice_check.py "Where can I build a Campus?" "What does a Ziggurat do?"
    .venv/bin/python tools/voice_check.py --wav my-question.wav

Each question is a real Claude call and counts against the monthly cap (about 2 cents each).
Nothing is saved: recordings and George's audio stay in memory.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from civspeak.speech import Voice, to_wav  # noqa: E402


def say(voice: Voice, text: str, speaker: str) -> bytes:
    voice.load()
    lang = "en-gb" if speaker.startswith("b") else "en-us"
    samples, rate = voice._kokoro.create(text, voice=speaker, speed=1.0, lang=lang)
    return to_wav(samples, rate)


def post(url: str, data: bytes, content_type: str):
    req = urllib.request.Request(url, data=data, headers={"Content-Type": content_type}, method="POST")
    return urllib.request.urlopen(req, timeout=120)


def check(base: str, wav: bytes, label: str) -> dict:
    started = time.monotonic()
    heard = json.loads(post(f"{base}/api/listen?speak=1", wav, "audio/wav").read())
    listened = time.monotonic() - started
    row = {"asked": label, "heard": heard["text"], "listen_s": round(listened, 2)}
    if not heard["text"]:
        return row
    asked = time.monotonic()
    body = json.dumps({"question": heard["text"], "speak": True, "spoken": True}).encode()
    says, entries, done = [], [], {}
    with post(f"{base}/api/ask", body, "application/json") as res:
        buffer = ""
        for line in res:
            buffer += line.decode()
            if not buffer.endswith("\n\n"):
                continue
            event = dict(x.split(": ", 1) for x in buffer.strip().splitlines())
            buffer = ""
            data = json.loads(event["data"])
            if event["event"] == "entries":
                entries = [e["title"] for e in data]
            elif event["event"] == "say":
                says.append((data["url"], time.monotonic()))
            elif event["event"] in ("done", "problem"):
                done = data
    first_audio = None
    for n, (url, _) in enumerate(says):
        clip = urllib.request.urlopen(base + url, timeout=120).read()
        if n == 0:
            first_audio = time.monotonic() - asked
        assert clip[:4] == b"RIFF", "speech is not a WAV"
    row.update(
        entries=entries[:4],
        answer=done.get("text") or done.get("message", ""),
        sentences=len(says),
        first_text_s=done.get("first_text_seconds"),
        first_voice_s=round(first_audio, 2) if first_audio is not None else None,
        speaking_starts_s=round(listened + first_audio, 2) if first_audio is not None else None,
        cost=done.get("cost"),
    )
    return row


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("questions", nargs="*")
    p.add_argument("--wav", action="append", default=[], help="a recorded question (16-bit WAV)")
    p.add_argument("--url", default="http://localhost:8765")
    p.add_argument("--voices", default="bm_lewis,am_michael,bf_emma", help="Kokoro voices to ask with, in turn")
    args = p.parse_args()
    voice = Voice()
    speakers = args.voices.split(",")
    jobs = [(Path(w).read_bytes(), w) for w in args.wav]
    jobs += [(say(voice, q, speakers[n % len(speakers)]), q) for n, q in enumerate(args.questions)]
    if not jobs:
        p.error("give questions to say, or --wav recordings")
    for wav, label in jobs:
        print(json.dumps(check(args.url, wav, label), ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
