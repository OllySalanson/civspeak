"""civspeak command line: build the index, ask questions, run the answer page."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import textwrap
import time
from pathlib import Path

from . import paths


def _nice() -> None:
    # The index build shares the PC with the game: always run it at low priority.
    try:
        os.nice(10)
    except OSError:
        pass


def ensure_index(force: bool = False, quiet: bool = False) -> Path:
    """Build or refresh the index when the game's cache or log changed since the last build."""
    from . import index

    out = paths.index_file()
    game = paths.discover()
    if force or index.is_stale(out, game):
        if not quiet:
            print("Building the Civilopedia index from your game (low priority)...", file=sys.stderr)
        _nice()
        report = index.build(game, out)
        if not quiet:
            print(
                f"Indexed {report.pages} pages from {report.text_files} text files "
                f"({report.tags} text entries) in {report.seconds:.1f}s.",
                file=sys.stderr,
            )
            if report.unresolved:
                print(f"{len(report.unresolved)} text files named in Modding.log were not found:", file=sys.stderr)
                for u in report.unresolved[:10]:
                    print(f"  {u}", file=sys.stderr)
    return out


def cmd_build(args) -> int:
    ensure_index(force=True)
    return 0


def cmd_ask(args) -> int:
    from .search import Index

    index = Index(ensure_index(quiet=args.json))
    question = " ".join(args.question)
    hits = index.search(question, k=args.k)
    if args.json:
        print(json.dumps([{"score": h.score, "why": h.why, **h.page} for h in hits], indent=2))
        return 0
    if not hits:
        print("No matching Civilopedia entries.")
        return 1
    width = min(100, os.get_terminal_size().columns if sys.stdout.isatty() else 100)
    for n, hit in enumerate(hits, 1):
        p = hit.page
        where = p["section"] + (f" - {p['group']}" if p["group"] else "")
        print(f"{n}. {p['title']}  [{where}]  score {hit.score} ({', '.join(hit.why)})")
        for g in p["facts"]:
            line = f"{g['heading']}: " + "; ".join(g["lines"]) if g["heading"] else "; ".join(g["lines"])
            print(textwrap.fill(line, width, initial_indent="   ", subsequent_indent="     "))
        chapters = p["chapters"] if args.full else p["chapters"][:1]
        for c in chapters:
            text = "\n".join(c["paragraphs"])
            if not args.full and len(text) > 400:
                text = text[:400].rsplit(" ", 1)[0] + " ..."
            label = f"{c['title']}: " if c["title"] else ""
            for para in (label + text).split("\n"):
                if para.strip():
                    print(textwrap.fill(para, width, initial_indent="   ", subsequent_indent="   "))
        print()
    return 0


def cmd_serve(args) -> int:
    from .server import serve

    # Speech recognition and the voice run in this process: keep them below everything else.
    _nice()
    return serve(port=args.port, open_browser=args.open, watch_stdin=args.watch_stdin)


def cmd_helper(args) -> int:
    from . import helper

    if args.action == "install":
        print(helper.install(port=args.port))
    elif args.action == "uninstall":
        print(helper.uninstall())
    else:
        print(helper.status())
    return 0


def cmd_status(args) -> int:
    from . import index, latency, spend

    out = paths.index_file()
    print(f"Index:      {out}")
    try:
        game = paths.discover()
        print(f"Game:       {game.game_dir}")
        print(f"Game cache: {game.gameplay_db}")
        if out.exists():
            meta = index.read_meta(out)
            loaded = time.strftime("%d %b %H:%M", time.localtime(float(meta["game_loaded_at"])))
            print(f"Built from the game you loaded at {loaded}; up to date: {not index.is_stale(out, game)}")
    except paths.SetupError as e:
        print(f"Game:       {e}")
    key = paths.key_file()
    print(f"API key:    {'present' if key.exists() and key.stat().st_size else 'missing'} ({key})")
    ledger = spend.Ledger(paths.spend_file())
    print(f"Spend:      ${ledger.month_total():.2f} of ${ledger.cap:.2f} this month")
    print(f"Speed:      {latency.summary(latency.LatencyLog(paths.latency_file()).recent(20))} ({paths.latency_file()})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="civspeak", description="Ask the Civ VI Civilopedia out loud.")
    parser.add_argument("-v", "--verbose", action="store_true", help="show warnings while building")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("build", help="rebuild the Civilopedia index from your game")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("ask", help="print the Civilopedia entries that match a question")
    p.add_argument("question", nargs="+")
    p.add_argument("-k", type=int, default=6, help="how many entries (default 6)")
    p.add_argument("--full", action="store_true", help="print every chapter in full")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("serve", help="run the answer page on http://localhost:PORT")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--open", action="store_true", help="open the page in Brave on Windows")
    p.add_argument("--watch-stdin", action="store_true", help="stop when standard input closes (used by the Windows helper)")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("helper", help="the Windows helper: hold ` in Civ VI to ask (install, uninstall, status)")
    p.add_argument("action", choices=["install", "uninstall", "status"])
    p.add_argument("--port", type=int, default=8765, help="the port the helper starts the page on (default 8765)")
    p.set_defaults(func=cmd_helper)

    p = sub.add_parser("status", help="show where things are, index age and this month's spend")
    p.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING if args.verbose else logging.ERROR, format="%(message)s")
    try:
        return args.func(args)
    except paths.SetupError as e:
        print(str(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
