#!/usr/bin/env python3
"""verify — check library records against the AUTHORITIES that issued their identifiers.

Resolves every DOI against Crossref and every arXiv id against the arXiv API, then compares the
stored title / authors / year / journal / volume / pages against what the authority returns.

WHAT IT IS HUNTING (2026-08-14). Not typos -- metadata that LOOKS right. Paraphrased
titles and truncated author lists are the signature of a record that was AUTHORED rather than
FETCHED, and a DOI wrong by two characters resolves to a real but UNRELATED paper. So:

    A TITLE THAT IS 90% SIMILAR IS A FAILURE, NOT A PASS.

Never writes to library.db, never invents a value, never "corrects" anything. It reports. The
only thing an LLM may not do to a citation is regenerate it, so this fetches or it says
"unverifiable" -- there is no third option.

Field classes, because not every disagreement is a defect:
  HARD (correctness): identifier resolves, title, first-author surname, year
  SOFT (convention):  journal name style (ApJ vs The Astrophysical Journal), volume, pages,
                      author-list length -- reported, but a difference here is usually house
                      style, not a wrong paper.
A record fails on any HARD field. SOFT-only differences are surfaced separately so the hard
signal is not drowned.

Usage:
  python3 verify.py --out audit.db                  # everything with an identifier (resumable)
  python3 verify.py --out audit.db --limit 50       # smoke run
  python3 verify.py --out audit.db --only doi       # or --only arxiv
  python3 verify.py --out audit.db --report         # print the report from an existing run
  python3 verify.py --selftest                      # comparator negative controls
"""
import argparse
import concurrent.futures
import difflib
import json
import os
import re
import sqlite3
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

EMAIL = os.environ.get("PAPERS_EMAIL", "papers-vela@example.com")
UA = {"User-Agent": "papers-vela-verify/1.0 (mailto:%s)" % EMAIL}

TITLE_HARD_FAIL = 0.90     # below this: different paper
TITLE_CLEAN = 0.98         # at or above: same string modulo punctuation/markup
                           # between the two: the PARAPHRASE BAND -- the 2026-08-14 signature


# ---------------------------------------------------------------- normalisation

def fold(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))


# NFKD decomposes accents but NOT these -- they are distinct letters, not letter+mark. Without
# an explicit map, 'Jørgensen' loses the ø to the non-letter strip and becomes 'jrgensen' on
# whichever side gets stripped, so the two sides stop matching. Measured 2026-08-14: ids 1208,
# 1377 flagged as wrong-author purely from this asymmetry.
TRANSLIT = {"ø": "o", "Ø": "o", "æ": "ae", "Æ": "ae", "å": "a", "Å": "a", "ð": "d", "Ð": "d",
            "þ": "th", "Þ": "th", "ł": "l", "Ł": "l", "đ": "d", "Đ": "d", "ß": "ss",
            "ı": "i", "œ": "oe", "Œ": "oe"}


# LaTeX accent escapes survive in BibTeX-sourced author fields: Garc\'{i}a-Hern\'{a}ndez,
# Mo\v{c}\'{a}k, Pomp\'{e}ia. The letter lives inside the braces, so stripping punctuation
# without unwrapping it first deletes the vowel -> 'garc'a-hern'andez', which then matches no
# real name. The AUTHORITY side is clean, so this shows up as a fabricated author mismatch.
# Measured 2026-08-14: ~10 records across the corpus.
LATEX_ACCENT = re.compile(r"\\[`'^\"~=.]\s*\{?([a-zA-Z])\}?")
LATEX_ACCENT_CMD = re.compile(r"\\(?:v|u|H|c|k|b|d|r|t)\s*\{?([a-zA-Z])\}?")


def norm_surname(s):
    """One normalisation, applied to BOTH sides. Any asymmetry here invents author mismatches."""
    s = s or ""
    s = LATEX_ACCENT.sub(r"\1", s)
    s = LATEX_ACCENT_CMD.sub(r"\1", s)
    for k, v in TRANSLIT.items():
        if k in s:
            s = s.replace(k, v)
    s = fold(s).lower()
    return re.sub(r"[^a-z'\-]", "", s)


def surname_key(s):
    """Comparison key that also collapses the German transliteration pair: a record may carry
    'Ruediger' where the authority has 'Rüdiger' (-> 'rudiger'). Both reduce to 'rudiger'."""
    s = norm_surname(s)
    for a, b in (("ue", "u"), ("oe", "o"), ("ae", "a")):
        s = s.replace(a, b)
    return s.replace("'", "").replace("-", "")


# Greek letters appear as symbols on one side and spelled out on the other -- 'ω Centauri' vs
# 'Omega Centauri' is the SAME title. Measured against live Crossref data 2026-08-14; without
# this the paraphrase band fills with false positives.
GREEK = {
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "ε": "epsilon", "ζ": "zeta",
    "η": "eta", "θ": "theta", "ι": "iota", "κ": "kappa", "λ": "lambda", "μ": "mu",
    "ν": "nu", "ξ": "xi", "ο": "omicron", "π": "pi", "ρ": "rho", "σ": "sigma", "ς": "sigma",
    "τ": "tau", "υ": "upsilon", "φ": "phi", "χ": "chi", "ψ": "psi", "ω": "omega",
    "Α": "alpha", "Β": "beta", "Γ": "gamma", "Δ": "delta", "Θ": "theta", "Λ": "lambda",
    "Ξ": "xi", "Π": "pi", "Σ": "sigma", "Φ": "phi", "Ψ": "psi", "Ω": "omega",
}


def strip_markup(s):
    """Strip markup but KEEP the text it wraps. Deleting `$...$` outright while HTML tags only
    lose their angle brackets makes the two sides normalise differently -- '$^{12}$C' would
    become 'C' and '<i>12</i>C' would become '12 C', and every title containing math would be
    reported as paraphrased. Both must reduce to the same '12 c'."""
    s = s or ""
    s = re.sub(r"<[^>]+>", " ", s)                      # HTML / MathML tags from Crossref
    # AAS/ADS legacy pseudo-markup that Crossref serves verbatim: 'Type I[CLC]a[/CLC]'. This is
    # dirt in the AUTHORITY, not in our record -- measured on live data, id 19.
    # An EXPLICIT tag list, never a generic [A-Z]+ run: astronomy titles are full of forbidden-
    # line notation ('[O III]', '[Fe II]', '[C II]') and a greedy pattern deletes it from one
    # side only, inventing a mismatch. Measured: norm_title('X [OIII] Y') came back 'x y'.
    s = re.sub(r"\[/?(CLC|ITAL|BF|SUB|SUP|RM|SC|TT|UL)\]", "", s, flags=re.I)
    s = s.replace("$", " ")                             # math delimiters, not the math
    s = re.sub(r"\\[a-zA-Z]+\s*", " ", s)               # LaTeX commands (\rm, \odot, ...)
    s = re.sub(r"[{}~^_]", " ", s)
    return s                                            # Greek handled in norm_title, which
                                                        # needs BOTH variants -- doing it here
                                                        # too would leave nothing to strip.


def norm_title(s, greek=True):
    s = strip_markup(s)
    if greek:
        for k, v in GREEK.items():
            if k in s:
                s = s.replace(k, " " + v + " ")
    else:
        for k in GREEK:
            if k in s:
                s = s.replace(k, " ")
    s = fold(s).lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def title_ratio(a, b):
    """Best of two normalisations. Transport strips Greek letters unpredictably -- Crossref
    returned 'Modelling the chemical evolution of   Centauri' for a paper whose real title is
    'of ω Centauri' (live id 17, 2026-08-14). Our record was right and the AUTHORITY was lossy.
    Expanding ω->'omega' then makes OUR side carry a word theirs lacks, which reads as a
    paraphrase. So compare both ways -- symbol expanded, and symbol removed from both sides --
    and take the better.

    The stripped fallback is allowed ONLY when one side has Greek and the other has none, i.e.
    the signature of transport LOSS. When both sides carry Greek letters that DIFFER, that is a
    real distinction -- omega Centauri and alpha Centauri are different objects -- and stripping
    both would make the comparator blind to it."""
    def ratio(g):
        x, y = norm_title(a, greek=g), norm_title(b, greek=g)
        return difflib.SequenceMatcher(None, x, y).ratio() if x and y else 0.0

    ga = {k for k in GREEK if k in (a or "")}
    gb = {k for k in GREEK if k in (b or "")}
    best = ratio(True)
    if bool(ga) != bool(gb):                       # exactly one side lost its symbols
        best = max(best, ratio(False))
    return best


def surnames(authors):
    """Extract surnames from any of the four conventions present in this library:
         papers3 : 'E Carretta, A Bragaglia, and V D'Orazi'      (First Last, comma-separated)
         bibtex  : 'Karinkuzhi, D. and Van Eck, S.'              (Last, First; ' and ' between)
         ads     : '{Glatzmaier}, G.~A.'                         (braced surname)
         plain   : 'Falk Herwig'
    Getting this wrong would flag the whole library, so it is selftested below."""
    s = fold(strip_markup(authors or "")).strip()
    if not s:
        return []
    s = re.sub(r"\bet\.?\s*al\.?", "", s, flags=re.I)
    chunks = re.split(r"\s+and\s+", s) if re.search(r"\s+and\s+", s) else [s]
    # BibTeX style iff every ' and '-separated chunk holds at most one comma.
    bibtex = len(chunks) > 1 and all(c.count(",") <= 1 for c in chunks)
    if len(chunks) == 1 and s.count(",") <= 1:
        bibtex = True                                    # single author, 'Last, First'
    out = []
    if bibtex:
        people = chunks
    else:
        people = [p for p in re.split(r",", s)]
    for p in people:
        p = re.sub(r"^\s*and\s+", "", p.strip()).strip(" .;")
        if not p or p.lower() in ("others", "et al"):
            continue
        if bibtex and "," in p:
            sur = p.split(",")[0]
        else:
            toks = [t for t in p.split() if t]
            sur = toks[-1] if toks else ""
        sur = norm_surname(sur)
        if sur:
            out.append(sur)
    return out


def norm_journal(s):
    s = fold(strip_markup(s)).lower()
    s = re.sub(r"\bthe\b|\bof\b|\band\b|\bfor\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def journal_ok(lib, auth):
    """True when the two名 the same journal. Abbreviation vs full name is house style, not a
    defect: 'ApJ' and 'The Astrophysical Journal' must not be reported as a mismatch."""
    a, b = norm_journal(lib), norm_journal(auth)
    if not a or not b:
        return True                                      # nothing to compare
    if a == b or a in b or b in a:
        return True
    # Astronomy abbreviations keep lowercase continuation letters -- ApJ, PASP, A&A -- so
    # first-letters-of-words does NOT reproduce them ('astrophysical journal' -> 'aj', not
    # 'apj'). Subsequence is the right test: a-p-j all occur, in order, in the full name.
    for short, full in ((a.replace(" ", ""), b.replace(" ", "")),
                        (b.replace(" ", ""), a.replace(" ", ""))):
        if 2 <= len(short) <= 8 and short[0] == full[:1]:
            it = iter(full)
            if all(ch in it for ch in short):
                return True
    # Word-wise abbreviation: 'Annu Rev Astro Astrophys' vs 'Annual Review of Astronomy and
    # Astrophysics' -- every short word is a prefix of the matching long word.
    for short, full in ((a.split(), b.split()), (b.split(), a.split())):
        if 1 < len(short) <= len(full) and all(
                any(f.startswith(s) for f in full) for s in short):
            return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.85


def norm_page(s):
    s = (s or "").strip()
    m = re.match(r"[A-Za-z]?0*(\d+)", s)
    return (re.match(r"([A-Za-z]?)", s).group(1).upper() + m.group(1)) if m else s.upper()


def pages_ok(lib, auth):
    if not lib or not auth:
        return True
    return norm_page(lib.split("-")[0]) == norm_page(auth.split("-")[0])


def norm_doi(s):
    s = (s or "").strip().lower()
    s = re.sub(r"^https?://(dx\.)?doi\.org/", "", s)
    # Publisher URL tails pasted into the DOI field: .../aab777/meta, .../ab01e1/pdf. The DOI is
    # real; the suffix is not part of it, and leaving it on makes a good record look unresolvable.
    s = re.sub(r"/(meta|pdf|full|abstract|html)$", "", s)
    return s.rstrip(".")


ARXIV_DOI = re.compile(r"^10\.48550/arxiv\.(.+)$", re.I)


def doi_is_arxiv(doi):
    """arXiv mints DOIs through DataCite, not Crossref, so Crossref 404s on every one of them.
    Reporting those as 'does not resolve' would condemn perfectly good records -- 61 of the 83
    unresolvables in the first full pass were exactly this. Route them to the arXiv API instead."""
    m = ARXIV_DOI.match(doi or "")
    return m.group(1) if m else None


def norm_arxiv(s):
    s = (s or "").strip().lower()
    s = re.sub(r"^(arxiv:)", "", s)
    s = re.sub(r"v\d+$", "", s)
    return s


# ---------------------------------------------------------------- authorities

_last_call = [0.0]
_lock = threading.Lock()


def throttle(min_gap):
    with _lock:
        gap = time.time() - _last_call[0]
        if gap < min_gap:
            time.sleep(min_gap - gap)
        _last_call[0] = time.time()


def http(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_crossref(doi):
    """-> (status, meta). status: ok | notfound | error:<reason>"""
    url = "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="") \
          + "?mailto=" + urllib.parse.quote(EMAIL)
    for attempt in range(3):
        try:
            throttle(0.06)
            msg = json.loads(http(url))["message"]
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return "notfound", None
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            return "error:http%d" % e.code, None
        except Exception as e:
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            return "error:%s" % type(e).__name__, None
    else:
        return "error:retries", None

    title = (msg.get("title") or [""])[0]
    auths = []
    for a in msg.get("author") or []:
        fam = a.get("family") or a.get("name") or ""
        if fam:
            auths.append(fam)
    year = None
    for key in ("published-print", "published-online", "issued", "created"):
        parts = (msg.get(key) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            year = int(parts[0][0])
            break
    # Full author strings too, so a record can be REBUILT from the source (not just checked).
    full = []
    for a in msg.get("author") or []:
        fam = a.get("family") or a.get("name") or ""
        giv = (a.get("given") or "").strip()
        if fam:
            full.append("%s, %s" % (fam, giv) if giv else fam)
    return "ok", {
        "title": title,
        "authors_full": " and ".join(full),
        "surnames": [norm_surname(x) for x in auths],
        "n_authors": len(auths),
        "year": year,
        "journal": (msg.get("container-title") or [""])[0],
        "volume": msg.get("volume") or "",
        "pages": msg.get("page") or "",
        "type": msg.get("type") or "",
    }


def fetch_arxiv_batch(ids, _depth=0):
    """arXiv API takes an id_list; batch to stay inside their rate guidance.

    RETRIES, then falls back to one-at-a-time. A single transient HTTP error used to mark the
    whole batch unresolvable -- 50 perfectly good records reported as missing from one blip,
    which reads as 50 defects. Measured 2026-08-14: all 50 resolved fine on retry."""
    out = {}
    url = ("http://export.arxiv.org/api/query?id_list=" + ",".join(ids)
           + "&max_results=%d" % len(ids))
    raw = None
    for attempt in range(3):
        try:
            throttle(3.0)
            raw = http(url, timeout=60)
            break
        except Exception as e:
            err = type(e).__name__
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
    if raw is None:
        if len(ids) > 1 and _depth == 0:
            for i in ids:                       # one bad id must not poison the rest
                out.update(fetch_arxiv_batch([i], _depth=1))
            return out
        for i in ids:
            out[i] = ("error:%s" % err, None)
        return out
    ns = {"a": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(raw)
    except Exception:
        for i in ids:
            out[i] = ("error:xml", None)
        return out
    for entry in root.findall("a:entry", ns):
        idtxt = (entry.findtext("a:id", "", ns) or "")
        aid = norm_arxiv(idtxt.rsplit("/abs/", 1)[-1])
        title = " ".join((entry.findtext("a:title", "", ns) or "").split())
        if not aid or title.lower().startswith("error"):
            continue
        names = [ (a.findtext("a:name", "", ns) or "") for a in entry.findall("a:author", ns)]
        surs = []
        for n in names:
            toks = (n or "").split()
            if toks:
                surs.append(norm_surname(toks[-1]))
        pub = entry.findtext("a:published", "", ns) or ""
        year = int(pub[:4]) if pub[:4].isdigit() else None
        out[aid] = ("ok", {"title": title, "surnames": surs, "n_authors": len(surs),
                           "year": year, "journal": "", "volume": "", "pages": "",
                           "type": "preprint"})
    for i in ids:
        out.setdefault(i, ("notfound", None))
    return out


# ---------------------------------------------------------------- comparison

def compare(paper, auth):
    """-> (verdict, hard[], soft[], ratio). verdict: ok | mismatch | soft."""
    hard, soft = [], []

    ratio = title_ratio(paper["title"], auth["title"])
    # AUTHORITY TRUNCATION, not paraphrase. Crossref registers some A&A series papers under the
    # bare series title -- 'The intermediate neutron capture process' for a paper whose real
    # title continues '. I. Development of the i-process in low-metallicity low-mass stars'.
    # Our record is the fuller and more correct one, and ADS confirms it at 1.00. Measured
    # 2026-08-14 on choplin2021 / choplin2022a / martinet2024. Flagging that as a defect trains
    # the reader to ignore the audit, which is worse than not running it.
    # A title field the AUTHORITY has corrupted carries no signal. Crossref serves the record for
    # 10.1086/307445 with a whole LaTeX preamble pasted into the title (\documentclass{aastex}
    # \usepackage{...}), which normalises to word salad and scores 0.34 against the correct
    # title. Judging the record on that would report a perfectly good citation as a wrong paper.
    # When the authority's title is unusable, fall back to author + year, which are intact.
    # Measured 2026-08-14 on jose1999 in paper_gammas.bib.
    if re.search(r"\\(documentclass|usepackage|begin\s*\{document\})", auth["title"] or "",
                 re.I):
        soft.append("title:authority-corrupt(LaTeX preamble in the authority record)")
        ratio = 1.0
        lt = at = ""
    else:
        lt, at = norm_title(paper["title"]), norm_title(auth["title"])
    _lib0 = (surnames(paper["authors"]) or [""])[0]
    _auth_sur = auth.get("surnames") or []
    _author_agrees = bool(_lib0) and any(
        surname_key(_lib0) and surname_key(_lib0) == surname_key(s) for s in _auth_sur)
    if (at and lt.startswith(at) and len(at) >= 20 and len(at) < len(lt)
            and _author_agrees):
        soft.append("title:authority-truncated(%d of %d chars)" % (len(at), len(lt)))
    elif ratio < TITLE_HARD_FAIL:
        hard.append("title:different(%.2f)" % ratio)
    elif ratio < TITLE_CLEAN:
        hard.append("title:paraphrased(%.2f)" % ratio)

    lib_sur = surnames(paper["authors"])
    a_sur = auth["surnames"]
    if lib_sur and a_sur:
        def same(x, y):
            x, y = surname_key(x), surname_key(y)             # accents, ue/ü, particles
            return bool(x) and bool(y) and (x == y or x.endswith(y) or y.endswith(x))

        if not same(lib_sur[0], a_sur[0]):
            # A first-author difference is only a HARD failure when the name is absent from the
            # authority's list ENTIRELY. Astronomy puts collective names first -- Crossref
            # returns 'The Astropy Collaboration' as author[0] with Robitaille second -- and a
            # strict positional test refuses a large share of legitimate modern papers.
            # Measured on 10.1051/0004-6361/201322068 (Astropy 2013), 2026-08-14.
            if any(same(lib_sur[0], s) for s in a_sur):
                soft.append("first-author:position(%s not first at authority)" % lib_sur[0])
            else:
                hard.append("first-author:%s absent from authority list (theirs: %s)"
                            % (lib_sur[0], a_sur[0]))
        if len(lib_sur) < len(a_sur):
            soft.append("authors:truncated(%d/%d)" % (len(lib_sur), len(a_sur)))
        elif len(lib_sur) > len(a_sur):
            soft.append("authors:extra(%d/%d)" % (len(lib_sur), len(a_sur)))
    elif a_sur and not lib_sur:
        hard.append("authors:missing")

    ly, ay = paper["year"], auth["year"]
    if ly and ay:
        if abs(int(ly) - int(ay)) == 1:
            soft.append("year:%s vs %s" % (ly, ay))       # online-first vs print is legitimate
        elif int(ly) != int(ay):
            hard.append("year:%s!=%s" % (ly, ay))

    if auth["journal"] and not journal_ok(paper["journal"], auth["journal"]):
        soft.append("journal:%s vs %s" % ((paper["journal"] or "-")[:28], auth["journal"][:28]))
    if paper["volume"] and auth["volume"] and paper["volume"].strip() != auth["volume"].strip():
        soft.append("volume:%s vs %s" % (paper["volume"], auth["volume"]))
    if not pages_ok(paper["pages"], auth["pages"]):
        soft.append("pages:%s vs %s" % (paper["pages"], auth["pages"]))

    verdict = "mismatch" if hard else ("soft" if soft else "ok")
    return verdict, hard, soft, ratio


# ---------------------------------------------------------------- run

RESULT_SCHEMA = """
CREATE TABLE IF NOT EXISTS results(
  paper_id INTEGER PRIMARY KEY, citekey TEXT, source TEXT, authority TEXT, ident TEXT,
  status TEXT,        -- ok | notfound | error:*
  verdict TEXT,       -- ok | soft | mismatch | unresolvable
  ratio REAL, hard TEXT, soft TEXT,
  lib_title TEXT, auth_title TEXT, checked_at TEXT,
  -- The authority's raw answer, cached. Fetching is the slow part and grading is free, so
  -- caching it means a comparator change is re-graded offline (--regrade) instead of costing
  -- another full pass over Crossref. Every comparator fix during this campaign otherwise
  -- forced a 10-minute re-fetch.
  auth_json TEXT);
CREATE INDEX IF NOT EXISTS ix_verdict ON results(verdict);
"""


def open_lib(path):
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True)     # READ-ONLY, always


def load_targets(libdb, only, limit):
    c = open_lib(libdb)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT id,citekey,source,title,authors,year,journal,volume,pages,doi,arxiv "
        "FROM papers ORDER BY id").fetchall()
    c.close()
    out = []
    for r in rows:
        d, a = norm_doi(r["doi"]), norm_arxiv(r["arxiv"])
        ad = doi_is_arxiv(d)
        if ad:                      # 10.48550/arxiv.NNNN is a DataCite DOI -> ask arXiv
            a = a or norm_arxiv(ad)
            d = ""
        if d and only in ("all", "doi"):
            out.append((dict(r), "crossref", d))
        elif a and only in ("all", "arxiv"):
            out.append((dict(r), "arxiv", a))
    return out[:limit] if limit else out


def run(libdb, outdb, only, limit, workers):
    res = sqlite3.connect(outdb)
    res.executescript(RESULT_SCHEMA)
    done = {r[0] for r in res.execute("SELECT paper_id FROM results")}
    targets = [t for t in load_targets(libdb, only, limit) if t[0]["id"] not in done]
    print("%d to check (%d already done)" % (len(targets), len(done)), flush=True)

    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    n = [0]

    def store(paper, authority, ident, status, meta):
        if status == "ok" and meta:
            verdict, hard, soft, ratio = compare(paper, meta)
            at = meta["title"]
        else:
            verdict = "unresolvable"
            hard, soft, ratio, at = ["identifier:%s" % status], [], 0.0, ""
        res.execute(
            "INSERT OR REPLACE INTO results VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (paper["id"], paper["citekey"], paper["source"], authority, ident, status,
             verdict, ratio, "; ".join(hard), "; ".join(soft),
             paper["title"], at, now, json.dumps(meta) if meta else None))
        n[0] += 1
        if n[0] % 50 == 0:
            res.commit()
            print("  %d/%d" % (n[0], len(targets)), flush=True)

    dois = [(p, i) for p, a, i in targets if a == "crossref"]
    arxs = [(p, i) for p, a, i in targets if a == "arxiv"]

    if dois:
        print("Crossref: %d DOIs" % len(dois), flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(fetch_crossref, i): (p, i) for p, i in dois}
            for f in concurrent.futures.as_completed(futs):
                p, i = futs[f]
                try:
                    status, meta = f.result()
                except Exception as e:
                    status, meta = "error:%s" % type(e).__name__, None
                store(p, "crossref", i, status, meta)
        res.commit()

    if arxs:
        print("arXiv: %d ids" % len(arxs), flush=True)
        BATCH = 50
        by_id = {}
        for p, i in arxs:
            by_id.setdefault(i, []).append(p)
        ids = list(by_id)
        for k in range(0, len(ids), BATCH):
            chunk = ids[k:k + BATCH]
            got = fetch_arxiv_batch(chunk)
            for i in chunk:
                status, meta = got.get(i, ("error:missing", None))
                for p in by_id[i]:
                    store(p, "arxiv", i, status, meta)
            res.commit()
            print("  arxiv %d/%d" % (min(k + BATCH, len(ids)), len(ids)), flush=True)

    res.commit()
    report(outdb)
    res.close()


def regrade(libdb, outdb):
    """Recompute every verdict from the CACHED authority response. No network. This is how a
    comparator fix gets validated against the full corpus in seconds instead of re-fetching."""
    lib = open_lib(libdb); lib.row_factory = sqlite3.Row
    papers = {r["id"]: dict(r) for r in lib.execute(
        "SELECT id,title,authors,year,journal,volume,pages FROM papers")}
    lib.close()
    res = sqlite3.connect(outdb); res.row_factory = sqlite3.Row
    rows = res.execute("SELECT paper_id,status,auth_json FROM results").fetchall()
    n = changed = 0
    for r in rows:
        if r["status"] != "ok" or not r["auth_json"]:
            continue
        meta = json.loads(r["auth_json"])
        p = papers.get(r["paper_id"])
        if not p:
            continue
        verdict, hard, soft, ratio = compare(p, meta)
        cur = res.execute("SELECT verdict FROM results WHERE paper_id=?",
                          (r["paper_id"],)).fetchone()["verdict"]
        if cur != verdict:
            changed += 1
        res.execute("UPDATE results SET verdict=?,ratio=?,hard=?,soft=? WHERE paper_id=?",
                    (verdict, ratio, "; ".join(hard), "; ".join(soft), r["paper_id"]))
        n += 1
    res.commit()
    print("regraded %d cached result(s); %d verdict(s) changed" % (n, changed))
    res.close()


def report(outdb):
    res = sqlite3.connect(outdb)
    res.row_factory = sqlite3.Row
    tot = res.execute("SELECT COUNT(*) n FROM results").fetchone()["n"]
    if not tot:
        print("no results yet")
        return
    print("\n=== VERIFICATION REPORT (%d checked) ===" % tot)
    print("\nverdict:")
    for r in res.execute("SELECT verdict, COUNT(*) n FROM results GROUP BY verdict ORDER BY n DESC"):
        print("  %-14s %5d  (%.1f%%)" % (r["verdict"], r["n"], 100.0 * r["n"] / tot))
    print("\nby source:")
    for r in res.execute(
            "SELECT source, COUNT(*) n, SUM(verdict='ok') ok, SUM(verdict='soft') soft, "
            "SUM(verdict='mismatch') mm, SUM(verdict='unresolvable') un "
            "FROM results GROUP BY source ORDER BY n DESC"):
        print("  %-9s n=%-5d ok=%-5d soft=%-5d MISMATCH=%-4d UNRESOLVABLE=%d"
              % (r["source"], r["n"], r["ok"], r["soft"], r["mm"], r["un"]))
    print("\nhard-failure kinds:")
    kinds = {}
    for r in res.execute("SELECT hard FROM results WHERE verdict='mismatch'"):
        for h in (r["hard"] or "").split("; "):
            k = h.split(":")[0] + ":" + h.split(":")[1].split("(")[0] if ":" in h else h
            kinds[k] = kinds.get(k, 0) + 1
    for k, v in sorted(kinds.items(), key=lambda x: -x[1]):
        print("  %-34s %d" % (k, v))
    print("\nunresolvable kinds:")
    for r in res.execute("SELECT status, COUNT(*) n FROM results WHERE verdict='unresolvable' "
                         "GROUP BY status ORDER BY n DESC"):
        print("  %-24s %d" % (r["status"], r["n"]))
    res.close()


# ---------------------------------------------------------------- selftest

def selftest():
    """Negative controls for the comparator. The author parser is the risky part: if it were
    wrong the whole library would flag, so every real convention in the DB is pinned here."""
    cases = []

    def eq(label, got, want):
        cases.append((label, got == want, "got=%r want=%r" % (got, want)))

    eq("papers3 surnames", surnames("E Carretta, A Bragaglia, and V D'Orazi"),
       ["carretta", "bragaglia", "d'orazi"])       # apostrophe kept: it is part of the surname
    eq("papers3 3-author", surnames("Falk Herwig, Sam M Austin, and John C. Lattanzio"),
       ["herwig", "austin", "lattanzio"])
    eq("bibtex surnames", surnames("Karinkuzhi, D. and Van Eck, S. and Goriely, S."),
       ["karinkuzhi", "van eck".replace(" ", ""), "goriely"])
    eq("ads braced", surnames("{Glatzmaier}, G.~A."), ["glatzmaier"])
    eq("plain single", surnames("Falk Herwig"), ["herwig"])
    eq("et al dropped", surnames("Reindl, N. and others"), ["reindl"])
    # LIVE regression (ids 1208, 1377): NFKD does not decompose ø/æ/å, so one side kept the
    # letter and the other dropped it -> 'jrgensen' vs 'jørgensen', a fabricated mismatch.
    # BOTH sides must now go through norm_surname().
    eq("Nordic o-slash both sides", norm_surname("Jørgensen"), norm_surname("Jorgensen"))
    eq("Hjørringgaard normalises", norm_surname("Hjørringgaard"), "hjorringgaard")
    eq("ae ligature", norm_surname("Næss"), "naess")
    eq("Polish l-stroke", norm_surname("Michałowski"), "michalowski")
    eq("accents still folded", norm_surname("Müller"), "muller")
    eq("distinct names still differ", norm_surname("Jørgensen") != norm_surname("Jensen"), True)
    # LIVE regression: LaTeX accent escapes ate the vowel -> 'garc'a-hern'andez'.
    eq("latex accent brace", norm_surname("Garc\\'{i}a-Hern\\'{a}ndez"), "garcia-hernandez")
    eq("latex accent no brace", norm_surname("Pomp\\'eia"), "pompeia")
    eq("latex caron", norm_surname("Mo\\v{c}\\'{a}k"), "mocak")
    eq("latex umlaut", norm_surname('K\\"{a}ppeler'), "kappeler")
    eq("ue/umlaut equivalence", surname_key("Ruediger"), surname_key("Rüdiger"))
    eq("accent fix keeps names distinct", surname_key("Garcia") != surname_key("Gracia"), True)

    eq("identical title", round(title_ratio("The Stingray Nebula", "The Stingray Nebula"), 2), 1.0)
    eq("markup-only diff", title_ratio("A $^{12}$C study", "A <i>12</i>C study") > TITLE_CLEAN, True)
    # NEGATIVE CONTROL: the 2026-08-14 signature must land in the paraphrase band, not pass.
    para = title_ratio("Discovery of a strong magnetic field in the Stingray nebula",
                       "Discovery of a strong magnetic field in the Stingray Nebula HD 44179")
    eq("paraphrase caught (not >=0.98)", para < TITLE_CLEAN, True)
    eq("different paper caught", title_ratio("Convective overshoot in massive stars",
                                             "A catalogue of quasar redshifts") < TITLE_HARD_FAIL, True)

    # Regressions found on LIVE Crossref data during the 30-record smoke run, 2026-08-14.
    # Each of these was a FALSE 'paraphrased' flag; without them the paraphrase band fills
    # with noise and the real signal drowns.
    eq("live id19: [CLC] publisher markup",
       title_ratio("High-Resolution Spectroscopy of Metal-rich Giants in omega Centauri: "
                   "First Indication of Type Ia Supernova Enrichment",
                   "High-Resolution Spectroscopy of Metal-rich Giants in omega Centauri: "
                   "First Indication of Type I[CLC]a[/CLC] Supernova Enrichment") >= TITLE_CLEAN,
       True)
    eq("live id22: Greek symbol vs name",
       title_ratio("Formation of Omega Centauri from an ancient nucleated dwarf galaxy",
                   "Formation of ω Centauri from an ancient nucleated dwarf galaxy")
       >= TITLE_CLEAN, True)
    eq("live id23: U+2010 hyphen",
       title_ratio("Multidimensional simulations of the accretion-induced collapse",
                   "Multidimensional Simulations of the Accretion‐induced Collapse")
       >= TITLE_CLEAN, True)
    eq("live id33: word-wise journal abbrev",
       journal_ok("Annu Rev Astro Astrophys",
                  "Annual Review of Astronomy and Astrophysics"), True)
    eq("live id17: authority DROPPED the Greek letter",
       title_ratio("Modelling the chemical evolution of ω Centauri using three-dimensional "
                   "hydrodynamical simulations",
                   "Modelling the chemical evolution of   Centauri using three-dimensional "
                   "hydrodynamical simulations") >= TITLE_CLEAN, True)
    # The [CLC] strip must NEVER eat forbidden-line notation -- measured: 'X [OIII] Y' had been
    # normalising to 'x y', deleting content from one side only.
    eq("[O III] survives normalisation", "iii" in norm_title("Emission in [OIII] regions"), True)
    eq("[Fe II] survives normalisation", "fe" in norm_title("Lines of [FeII] in shocks"), True)
    eq("[CLC] still stripped", norm_title("Type I[CLC]a[/CLC] SNe"), "type ia sne")

    # ...but the fixes must not make the comparator blind: these still have to fail.
    eq("Greek fix does not merge different letters",
       title_ratio("Formation of ω Centauri", "Formation of α Centauri")
       < TITLE_CLEAN, True)
    eq("abbrev fix does not merge different journals",
       journal_ok("Annu Rev Astro Astrophys", "Annual Review of Biochemistry"), False)

    eq("ApJ == Astrophysical Journal", journal_ok("ApJ", "The Astrophysical Journal"), True)
    eq("MNRAS == Monthly Notices", journal_ok("MNRAS", "Monthly Notices of the Royal "
                                              "Astronomical Society"), True)
    eq("Nature != ApJ", journal_ok("Nature", "The Astrophysical Journal"), False)
    eq("pages 25802 == 025802", pages_ok("25802", "025802"), True)
    eq("pages L14 == L14", pages_ok("L14", "L14"), True)
    eq("pages 100 != 200", pages_ok("100", "200"), False)
    eq("doi url stripped", norm_doi("https://doi.org/10.1086/305772"), "10.1086/305772")
    eq("arxiv version stripped", norm_arxiv("arXiv:0801.1234v2"), "0801.1234")

    # NEGATIVE CONTROL on compare(): a deliberately wrong record MUST come back 'mismatch'.
    good = {"title": "Discovery of a strong magnetic field in the Stingray nebula",
            "authors": "Reindl, N. and Schaffenroth, V.", "year": 2017,
            "journal": "MNRAS", "volume": "464", "pages": "L51"}
    auth = {"title": "Discovery of a strong magnetic field in the Stingray nebula",
            "surnames": ["reindl", "schaffenroth"], "n_authors": 2, "year": 2017,
            "journal": "Monthly Notices of the Royal Astronomical Society: Letters",
            "volume": "464", "pages": "L51", "type": "journal-article"}
    eq("clean record -> ok", compare(good, auth)[0], "ok")
    wrong_title = dict(good, title="Magnetic fields in planetary nebulae: a survey")
    eq("wrong title -> mismatch", compare(wrong_title, auth)[0], "mismatch")
    wrong_auth = dict(good, authors="Guerrero, M. A.")
    eq("wrong first author -> mismatch", compare(wrong_auth, auth)[0], "mismatch")
    # LIVE regression: consortium first-author. Crossref returns 'The Astropy Collaboration'
    # as author[0]; our record starts at Robitaille. Same paper -- must NOT be refused.
    astropy_auth = {"title": "Astropy: A community Python package for astronomy",
                    "surnames": ["the astropy collaboration", "robitaille", "tollerud"],
                    "n_authors": 45, "year": 2013, "journal": "Astronomy & Astrophysics",
                    "volume": "558", "pages": "A33", "type": "journal-article"}
    astropy_lib = {"title": "Astropy: A community Python package for astronomy",
                   "authors": "Robitaille, T. P. and Tollerud, E. J.", "year": 2013,
                   "journal": "A&A", "volume": "558", "pages": "A33"}
    eq("consortium first author -> soft, not mismatch",
       compare(astropy_lib, astropy_auth)[0], "soft")
    # ...but a name absent from the list entirely must STILL be a hard failure.
    eq("absent author still mismatch",
       compare(dict(astropy_lib, authors="Askar, A."), astropy_auth)[0], "mismatch")
    wrong_year = dict(good, year=2011)
    eq("wrong year -> mismatch", compare(wrong_year, auth)[0], "mismatch")
    trunc = dict(good, authors="Reindl, N.")
    eq("truncated authors -> soft", compare(trunc, auth)[0], "soft")

    # LIVE regression (choplin2021 / martinet2024 / choplin2022a, 2026-08-14): Crossref serves
    # the bare A&A series title while our record carries the full one. Ours is MORE correct;
    # calling it a mismatch teaches the reader to ignore the audit.
    series_lib = {"title": "The intermediate neutron capture process. I. Development of the "
                           "i-process in low-metallicity low-mass stars",
                  "authors": "Choplin, A.", "year": 2021, "journal": "A&A",
                  "volume": "648", "pages": "A119"}
    series_auth = {"title": "The intermediate neutron capture process",
                   "surnames": ["choplin"], "n_authors": 1, "year": 2021,
                   "journal": "Astronomy & Astrophysics", "volume": "648", "pages": "A119",
                   "type": "journal-article"}
    eq("authority-truncated title -> soft", compare(series_lib, series_auth)[0], "soft")
    # LIVE regression (jose1999, paper_gammas.bib): Crossref pasted a LaTeX preamble into the
    # title field. The citation is correct; the AUTHORITY record is broken.
    jose_lib = {"title": "Nuclear Uncertainties in the NeNa-MgAl Cycles and Production of "
                         "$^{22}$Na and $^{26}$Al during Nova Outbursts",
                "authors": "Jose, J. and Coc, A.", "year": 1999, "journal": "ApJ",
                "volume": "520", "pages": "347"}
    jose_auth = {"title": "Nuclear Uncertainties in the NeNa-MgAl Cycles and Production of "
                          "\\documentclass{aastex} \\usepackage{amsbsy} 22Na",
                 "surnames": ["jose"], "n_authors": 2, "year": 1999,
                 "journal": "The Astrophysical Journal", "volume": "520", "pages": "347",
                 "type": "journal-article"}
    eq("authority LaTeX-preamble title -> not mismatch",
       compare(jose_lib, jose_auth)[0] != "mismatch", True)
    eq("corrupt title + wrong author -> mismatch",
       compare(dict(jose_lib, authors="Askar, A."), jose_auth)[0], "mismatch")
    eq("unrelated title still mismatch",
       compare(dict(series_lib, title="A catalogue of quasar redshifts"), series_auth)[0],
       "mismatch")
    # The prefix shortcut is only allowed when the author ALSO agrees; otherwise a record whose
    # title merely starts with the authority's could ride through it.
    eq("prefix + wrong author still mismatch",
       compare(dict(series_lib, authors="Askar, A."), series_auth)[0], "mismatch")

    bad = sum(1 for _, ok, _ in cases if not ok)
    for label, ok, detail in cases:
        print("%s  %-38s %s" % ("PASS" if ok else "FAIL", label, "" if ok else detail))
    print("\n%d/%d passed" % (len(cases) - bad, len(cases)))
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lib", default=None, help="library dir (default: the one papers.py resolves)")
    ap.add_argument("--out", default="audit.db")
    ap.add_argument("--only", choices=["all", "doi", "arxiv"], default="all")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--regrade", action="store_true",
                    help="recompute verdicts from cached authority data (no network)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if not a.lib:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import papers
        a.lib = papers.LIB
    if a.selftest:
        return selftest()
    if a.regrade:
        regrade(os.path.join(a.lib, "library.db"), a.out)
        report(a.out)
        return 0
    if a.report:
        report(a.out)
        return 0
    libdb = os.path.join(a.lib, "library.db")
    if not os.path.exists(libdb):
        sys.exit("no library.db at %s" % libdb)
    run(libdb, a.out, a.only, a.limit, a.workers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
