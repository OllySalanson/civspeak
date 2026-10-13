"""Sound-alike keys, so misheard or misspelt names still find their page.

A small Metaphone-style reduction tuned for Civ names: "Kongo"/"Congo", "Hansa"/"hanza",
"Mbanza"/"M-Bansa" and "Ziggurat"/"zigurat" all reduce to the same key.
"""

from __future__ import annotations

import re
import unicodedata

_DIGRAPHS = [
    ("x", "ks"),
    ("tch", "x"),
    ("sch", "sk"),
    ("ph", "f"),
    ("ck", "k"),
    ("gh", ""),
    ("kn", "n"),
    ("wr", "r"),
    ("qu", "kw"),
    ("sh", "x"),
    ("ch", "x"),
    ("th", "0"),
    ("dg", "j"),
]


def ascii_fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def key(word: str) -> str:
    w = re.sub(r"[^a-z]", "", ascii_fold(word).lower())
    if not w:
        return ""
    first = w[0]
    for a, b in _DIGRAPHS:
        w = w.replace(a, b)
    w = re.sub(r"c(?=[eiy])", "s", w)
    w = w.replace("c", "k").replace("q", "k").replace("z", "s").replace("v", "f").replace("d", "t")
    w = re.sub(r"g(?=[eiy])", "j", w)
    w = re.sub(r"(?<=[aeiou])[wy]|[wy](?![aeiou])", "", w)
    w = w[0] + w[1:].replace("h", "")
    w = re.sub(r"(.)\1+", r"\1", w)
    head, tail = w[0], re.sub(r"[aeiou]", "", w[1:])
    if first in "aeiou":
        head = "a"  # every leading vowel sounds alike when misheard
    return head + tail


def phrase_key(text: str) -> str:
    return " ".join(k for k in (key(t) for t in re.split(r"[\s\-']+", text)) if k)
