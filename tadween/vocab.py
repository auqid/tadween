"""Corrections that propagate: fix a word once, fix it everywhere - and in future calls."""
import difflib
import re
import string
import threading
import time
import unicodedata

from . import config

PUNCT = string.punctuation + "“”‘’…"


def pattern(phrase, case_sensitive=False):
    """Whole-word match for a word or phrase, tolerant of extra whitespace. A possessive 's still counts."""
    words = [re.escape(w) for w in phrase.split()]
    if not words:
        raise ValueError("empty phrase")
    body = r"\s+".join(words)
    return re.compile(rf"(?<![\w'’]){body}(?!\w)(?!['’](?![sS]\b)\w)", 0 if case_sensitive else re.IGNORECASE)


def _whole(m):
    """re's \\w leaves out combining marks (Hindi vowel signs, Arabic harakat): a match touching one is in a word."""
    s = m.string
    return not any(unicodedata.category(c)[0] == "M" for c in s[m.start() - 1:m.start()] + s[m.end():m.end() + 1])


def _keep_case(found, replacement, sentence_start):
    """A lowercase replacement keeps the capital of a sentence start."""
    if replacement.islower() and found[:1].isupper() and sentence_start:
        return replacement[:1].upper() + replacement[1:]
    return replacement


def replace(text, phrase, replacement):
    rx, count = pattern(phrase), 0
    if not rx.search(text):
        return text, 0
    # Spots that already read as the replacement stay ("Node.js" when fixing "Node" -> "Node.js").
    done = [m.span() for m in re.finditer(r"\s+".join(map(re.escape, replacement.split())), text, re.IGNORECASE)]

    def sub(m):
        nonlocal count
        a, b = m.span()
        if not _whole(m) or any(x <= a and b <= y and y - x > b - a for x, y in done):
            return m.group(0)
        before = text[:a].rstrip()
        new = _keep_case(m.group(0), replacement, not before or before.endswith((".", "?", "!")))
        count += new != m.group(0)  # already-correct spellings don't count
        return new

    return rx.sub(sub, text), count


def occurrences(turns, phrase, context=40):
    rx = pattern(phrase)
    hits = []
    for t in turns:
        for m in filter(_whole, rx.finditer(t["text"])):
            hits.append({"turn": t["id"], "start": t["start"], "match": m.group(0),
                         "before": t["text"][max(0, m.start() - context):m.start()],
                         "after": t["text"][m.end():m.end() + context]})
    return hits


def suggestions(old, new):
    """Word swaps made in an edit ("major now" -> "Measure Now") that could apply elsewhere too."""
    a, b = old.split(), new.split()
    key = lambda w: w.strip(PUNCT)  # noqa: E731 - punctuation-only edits are not word fixes
    found = []
    ops = difflib.SequenceMatcher(a=[key(w) for w in a], b=[key(w) for w in b], autojunk=False).get_opcodes()
    for op, i1, i2, j1, j2 in ops:
        if op != "replace" or i2 - i1 > 4 or j2 - j1 > 4:
            continue
        frm = " ".join(a[i1:i2]).strip(PUNCT)
        to = " ".join(b[j1:j2]).strip(PUNCT)
        if not frm or not to or frm == to:
            continue
        sentence_start = i1 == 0 or a[i1 - 1].endswith((".", "?", "!"))
        if sentence_start and frm.lower() == to.lower() and to == frm[:1].upper() + frm[1:]:
            continue  # just capitalised the first word of a sentence
        found.append({"from": frm, "to": to})
    return list({(s["from"], s["to"]): s for s in found}.values())


class Vocabulary:
    """data/vocabulary.json: remembered corrections plus extra terms that steer Whisper."""
    _lock = threading.Lock()

    def __init__(self):
        self.file = config.DATA / "vocabulary.json"
        self.data = config.read_json(self.file, {"corrections": [], "terms": []})

    def _save(self):
        config.write_json(self.file, self.data)

    def add_correction(self, frm, to):
        with self._lock:
            rules = [r for r in self.data["corrections"] if r["from"].lower() != frm.lower()]
            rules.append({"from": frm, "to": to, "added": time.time()})
            self.data["corrections"] = rules
            self._save()

    def remove_correction(self, frm):
        with self._lock:
            self.data["corrections"] = [r for r in self.data["corrections"] if r["from"].lower() != frm.lower()]
            self._save()

    def set_terms(self, terms):
        with self._lock:
            self.data["terms"] = [t.strip() for t in dict.fromkeys(terms) if t.strip()]
            self._save()

    def apply(self, text):
        """Run every remembered correction over a text -> (text, [rules that fired])."""
        fired = []
        for rule in self.data["corrections"]:
            text, n = replace(text, rule["from"], rule["to"])
            if n:
                fired.append(rule["to"])
        return text, fired

    def prompt_terms(self):
        return self.data["terms"] + [r["to"] for r in self.data["corrections"]]
