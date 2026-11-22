"""Ask the same Civilopedia questions of each answer model (Settings > Answers) and write the answers
side by side, with what each cost and how soon its first words came, for a person to judge.

Every answer is a real Claude call with your key, counted in the spend ledger like any other
question; the run stops before going over --max-usd. The report quotes your game's Civilopedia, so
write it outside the repo (the default is the data folder).

    .venv/bin/python tools/answer_check.py                         # 30 questions, every model
    .venv/bin/python tools/answer_check.py --limit 3 --models quick  # a quick look
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from civspeak import answer, index, paths, spend
from civspeak.search import Index

# Typed the way players ask, spoken or not: rules, placement, effects, names Whisper might mangle.
QUESTIONS = [
    "Where can I build a Campus?",
    "What does a Ziggurat do?",
    "How do I get Great Prophets?",
    "What does the Mbanza replace?",
    "How much does a Settler cost?",
    "What do I need to build a Harbor?",
    "What does the Colosseum wonder give?",
    "How does the Holy Site adjacency work?",
    "What unit is unique to Rome?",
    "What does Cleopatra's ability do?",
    "Can I build a Farm on a Hill?",
    "What does the Industrial Zone give next to a Mine?",
    "How strong is a Knight?",
    "What does Monarchy give?",
    "How do I found a religion?",
    "What is the Kongo's civilization ability?",
    "Where does Machu Picchu have to go?",
    "What is a Great Merchant good for?",
    "What does an Encampment do?",
    "Which resource does a Swordsman need?",
    "What does the Printing tech unlock?",
    "How do I get Tourism?",
    "What is a city-state suzerain?",
    "What does the Theater Square need to be built?",
    "What does Pillage do?",
    "How do Loyalty and Governors work?",
    "What does a Builder do?",
    "What does the Pyramids wonder give?",
    "What is the strongest naval unit in the Medieval era?",
    "What does Stonehenge need?",
]


def run(questions: list[str], choices: list[str], max_usd: float, out: Path) -> int:
    idx = Index(index.current())
    ledger = spend.Ledger(paths.spend_file())
    started_total = ledger.month_total()
    rows = []
    for n, question in enumerate(questions, 1):
        hits = idx.search(question)
        row = {"question": question, "entries": [h.page["title"] for h in hits], "answers": {}}
        for choice in choices:
            if ledger.month_total() - started_total >= max_usd:
                print(f"Stopping: ${max_usd:.2f} spent on this check.", file=sys.stderr)
                return _write(rows, out, ledger.month_total() - started_total)
            asker = answer.Answerer(ledger, choice=choice)
            began = time.monotonic()
            first = None
            result = None
            try:
                for kind, value in asker.stream(question, hits):
                    if kind == "delta" and first is None:
                        first = time.monotonic() - began
                    elif kind == "done":
                        result = value
            except answer.AnswerError as e:
                row["answers"][choice] = {"error": str(e)}
                continue
            row["answers"][choice] = {
                "model": result.model,
                "text": result.text,
                "used": result.used,
                "cost": round(result.cost_usd, 6),
                "first_words_s": round(first or 0, 2),
                "all_s": round(time.monotonic() - began, 2),
                "refused": result.refused,
            }
        rows.append(row)
        print(f"{n}/{len(questions)} {question}", file=sys.stderr)
    return _write(rows, out, ledger.month_total() - started_total)


def _write(rows: list[dict], out: Path, spent: float) -> int:
    out.write_text(json.dumps({"spent": round(spent, 4), "rows": rows}, indent=2), encoding="utf-8")
    summary = {}
    for row in rows:
        for choice, a in row["answers"].items():
            s = summary.setdefault(choice, {"n": 0, "cost": 0.0, "first": 0.0, "errors": 0})
            if "error" in a:
                s["errors"] += 1
                continue
            s["n"] += 1
            s["cost"] += a["cost"]
            s["first"] += a["first_words_s"]
    for choice, s in summary.items():
        if s["n"]:
            print(
                f"{choice:9} {s['n']} answers, ${s['cost'] / s['n']:.5f} each on average, "
                f"first words after {s['first'] / s['n']:.1f}s on average, {s['errors']} errors"
            )
    print(f"Spent ${spent:.4f} in all. Answers side by side: {out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--models", nargs="+", choices=list(answer.CHOICES), default=list(answer.CHOICES))
    parser.add_argument("--start", type=int, default=1, help="begin at question N (to finish an earlier run)")
    parser.add_argument("--limit", type=int, default=len(QUESTIONS), help="ask at most N questions")
    parser.add_argument("--max-usd", type=float, default=1.0, help="stop before this check spends more (default $1)")
    parser.add_argument("--out", type=Path, default=None, help="where to write the answers (JSON)")
    args = parser.parse_args()
    out = args.out or paths.data_dir() / "answer-check.json"
    first = max(1, args.start) - 1
    return run(QUESTIONS[first : first + args.limit], args.models, args.max_usd, out)


if __name__ == "__main__":
    sys.exit(main())
