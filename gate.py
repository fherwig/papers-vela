#!/usr/bin/env python3
"""gate — nothing enters the library unless an AUTHORITY agrees with it.

Admission validation for `papers add` and `import_bib.py`. Shares its comparator with verify.py
on purpose: the gate and the audit must never be able to disagree about what "correct" means.

=== THE DESIGN FACT, measured 2026-08-14 ===

The two wrong DOIs in the 2026-08-14 manuscript BOTH RESOLVE:

    10.1093/mnrasl/slw175  -> Reindl 2017, "the central star of the Stingray Nebula"   CORRECT
    10.1093/mnrasl/slw177  -> Askar 2017,  "MOCCA-SURVEY ... binary black holes"       WRONG
    10.1093/mnras/staa343  -> Evans 2020,  "V4334 Sgr (Sakurai's object)"              CORRECT
    10.1093/mnras/staa326  -> Blackman 2020, "Cloud fragmentation cascades"            WRONG

A "does the DOI resolve?" check passes all four. Only comparing the RETURNED METADATA against
the record catches the two that point at unrelated papers. So this gate never asks whether an
identifier resolves; it asks whether the paper on the other end is the paper you claim it is.

=== PROHIBITION, per agent-roster/agents.md "Choosing a guard's mode" ===

An error in the library is reversible; an error that ships in an accepted manuscript is not, and
that asymmetry is the whole reason this exists. So admission is REFUSED on:

    - metadata that disagrees with the authority on a HARD field (title / first author / year)
    - an identifier that does not resolve
    - an authority that cannot be reached (fail CLOSED -- a network blip must not become a
      silently unverified record; the message says it is transient and offers the override)
    - no identifier at all

with exactly ONE way through: --force-unverified "<reason>", which RECORDS the reason in the
`verification` table. Never a silent bypass, and `papers audit` lists every override.
"""
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verify as V

VERIFICATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS verification(
  paper_id INTEGER PRIMARY KEY,
  status TEXT,           -- verified | soft | override | unverified
  authority TEXT,        -- crossref | arxiv | none
  ident TEXT,
  ratio REAL,
  detail TEXT,           -- hard/soft findings, or the failure reason
  override_reason TEXT,  -- non-null ONLY for status='override'
  checked_at TEXT);
CREATE INDEX IF NOT EXISTS ix_verif_status ON verification(status);
"""


def ensure_table(conn):
    conn.executescript(VERIFICATION_SCHEMA)


def validate(rec):
    """rec: dict with title/authors/year/journal/volume/pages/doi/arxiv.

    -> (ok, status, authority, ident, ratio, detail)
       ok=False means REFUSE. status is one of:
         verified          hard fields agree with the authority
         soft              agrees on hard fields; convention-level differences noted
         mismatch          disagrees on a hard field  -> refuse
         unresolvable      identifier does not resolve -> refuse
         unreachable       authority could not be queried -> refuse (transient)
         no-identifier     nothing to check against -> refuse
    """
    doi = V.norm_doi(rec.get("doi"))
    arx = V.norm_arxiv(rec.get("arxiv"))
    _ad = V.doi_is_arxiv(doi)          # arXiv DOIs are DataCite; Crossref 404s on them
    if _ad:
        arx, doi = arx or V.norm_arxiv(_ad), ""

    if doi:
        authority, ident = "crossref", doi
        status, meta = V.fetch_crossref(doi)
    elif arx:
        authority, ident = "arxiv", arx
        got = V.fetch_arxiv_batch([arx])
        status, meta = got.get(arx, ("error:missing", None))
    else:
        return False, "no-identifier", "none", "", 0.0, "record carries neither DOI nor arXiv id"

    if status == "notfound":
        return (False, "unresolvable", authority, ident, 0.0,
                "%s does not resolve at %s" % (ident, authority))
    if status != "ok" or not meta:
        return (False, "unreachable", authority, ident, 0.0,
                "could not reach %s (%s)" % (authority, status))

    paper = {k: rec.get(k) for k in
             ("title", "authors", "year", "journal", "volume", "pages")}
    verdict, hard, soft, ratio = V.compare(paper, meta)
    detail = "; ".join(hard + soft)
    if verdict == "mismatch":
        return (False, "mismatch", authority, ident, ratio,
                detail + " | authority title: " + meta["title"][:110])
    return True, ("soft" if soft else "verified"), authority, ident, ratio, detail


def record(conn, paper_id, status, authority, ident, ratio, detail, override_reason=None):
    ensure_table(conn)
    conn.execute(
        "INSERT OR REPLACE INTO verification VALUES(?,?,?,?,?,?,?,?)",
        (paper_id, status, authority, ident, ratio, detail, override_reason,
         time.strftime("%Y-%m-%dT%H:%M:%S")))


def refusal_message(status, ident, detail, ctx=""):
    head = {
        "mismatch": "REFUSED: the authority disagrees with this record.",
        "unresolvable": "REFUSED: that identifier does not resolve.",
        "unreachable": "REFUSED: could not reach the authority to check this.",
        "no-identifier": "REFUSED: no DOI or arXiv id, so nothing can verify it.",
    }.get(status, "REFUSED.")
    body = ["", head, ""]
    if ctx:
        body.append("  " + ctx)
    if ident:
        body.append("  identifier: " + ident)
    if detail:
        body.append("  finding:    " + detail)
    body.append("")
    if status == "mismatch":
        body += [
            "  A DOI that is wrong by two characters still RESOLVES -- it just resolves to a",
            "  different paper. That is exactly how four wrong citations reached an accepted",
            "  manuscript. Check the identifier against the paper you mean.",
        ]
    elif status == "unreachable":
        body += ["  This is usually transient. Re-run before overriding."]
    elif status == "no-identifier":
        body += [
            "  Books, theses, private communications and old proceedings genuinely have none.",
            "  Those are the case the override exists for.",
        ]
    body += [
        "",
        "  If this is a genuine exception, declare it -- it is recorded, not waved through:",
        '      --force-unverified "<why this cannot be verified>"',
        "",
    ]
    return "\n".join(body)


# ------------------------------------------------------------------ selftest

def selftest():
    """LIVE negative controls built from the actual incident. Requires network."""
    cases = []

    def check(label, rec, want_ok, want_status=None):
        """A case whose AUTHORITY could not be reached is SKIPPED, not failed.

        This selftest makes live calls, so a network blip would otherwise report
        'GATE SELFTEST FAILED — do not ship' for a gate that is perfectly fine. Observed once on
        2026-08-14. A test that fails at random gets ignored exactly like a guard that fires at
        random, so it needs the same distinction the gate itself makes: could-not-ask is not a
        verdict."""
        ok, status, auth, ident, ratio, detail = validate(rec)
        if status == "unreachable" and want_status != "unreachable":
            cases.append((label, None, "SKIPPED — could not reach %s (%s)" % (auth, detail[:50])))
            return
        good = (ok == want_ok) and (want_status is None or status == want_status)
        cases.append((label, good, "ok=%s status=%s %s" % (ok, status, detail[:70])))

    stingray = {"title": "Breaking news from the HST: the central star of the Stingray Nebula "
                         "is now returning towards the AGB",
                "authors": "Reindl, N. and Schaffenroth, V. and Miller Bertolami, M. M.",
                "year": 2017, "journal": "MNRAS Letters", "volume": "", "pages": ""}

    # MUST PASS: the value the library actually holds.
    check("library DOI slw175 -> admitted", dict(stingray, doi="10.1093/mnrasl/slw175"), True)

    # MUST REFUSE: the value the manuscript held. It RESOLVES -- to an unrelated paper.
    check("wrong DOI slw177 -> REFUSED", dict(stingray, doi="10.1093/mnrasl/slw177"),
          False, "mismatch")

    sakurai = {"title": "The infrared view of dust and molecules around V4334 Sgr "
                        "(Sakurai's object): a 20-yr retrospective",
               "authors": "Evans, A. and Geballe, T. R.", "year": 2020,
               "journal": "MNRAS", "volume": "", "pages": ""}
    check("library DOI staa343 -> admitted", dict(sakurai, doi="10.1093/mnras/staa343"), True)
    check("wrong DOI staa326 -> REFUSED", dict(sakurai, doi="10.1093/mnras/staa326"),
          False, "mismatch")

    # MUST REFUSE: a DOI that denotes no paper at all (the fourth failure mode).
    check("nonexistent DOI -> REFUSED", dict(stingray, doi="10.1093/mnras/stz99999999"),
          False, "unresolvable")

    # MUST REFUSE: nothing to check against.
    check("no identifier -> REFUSED", dict(stingray), False, "no-identifier")

    # MUST REFUSE: a paraphrased title on an otherwise correct DOI -- the LLM signature.
    check("paraphrased title -> REFUSED",
          dict(stingray, doi="10.1093/mnrasl/slw175",
               title="News from Hubble: the Stingray Nebula central star returns toward the AGB"),
          False, "mismatch")

    # MUST PASS: an arXiv-only record.
    check("arXiv id -> admitted",
          {"title": "The R-process Alliance: First Release from the Southern Search for "
                    "R-process-enhanced Stars in the Galactic Halo",
           "authors": "Hansen, T. T.", "year": 2018, "journal": "", "volume": "", "pages": "",
           "arxiv": "1804.03114"}, True)

    bad = sum(1 for _, ok, _ in cases if ok is False)
    skipped = sum(1 for _, ok, _ in cases if ok is None)
    for label, ok, detail in cases:
        tag = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
        print("%s  %-38s %s" % (tag, label, "" if ok is True else detail))
    print("\n%d/%d passed%s"
          % (len(cases) - bad - skipped, len(cases) - skipped,
             (" (%d skipped: authority unreachable)" % skipped) if skipped else ""))
    if bad:
        print("GATE SELFTEST FAILED — do not ship.")
    elif skipped:
        print("Some cases could not run. Re-run when the network is up before trusting this.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(selftest() if "--selftest" in sys.argv else 0)
