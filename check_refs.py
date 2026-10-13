#!/usr/bin/env python3
r"""check_refs — verify the references in a document you did NOT write. READ-ONLY.

The referee case: a manuscript under review can carry exactly the 2026-08-14 signature, and no
reviewer checks sixty DOIs by hand. From our own incident we know the wrong
ones RESOLVE CLEANLY -- `10.1093/mnrasl/slw177` returns a real 2017 MNRAS paper, it
is simply a different one. That is the failure a human reviewer never catches, and it is the one
this command exists for.

=== IT MUST NEVER TOUCH THE LIBRARY ===

No insert, no tag, no `references.bib` rewrite, no cache written into $PAPERS_LIB. Somebody else's
bibliography must never become our records by side effect -- that would be a worse failure than
the one this catches, and the kind discovered years later. Structurally enforced: this module
never opens library.db, and `papers check-refs` is exempt from require_lib() so it runs with no
library configured at all.

=== CONFIDENTIALITY -- read this before checking a manuscript under review ===

Checking a document sends its DOIs, arXiv ids and titles to Crossref and NASA ADS. Individually
those are public identifiers and leak nothing. COLLECTIVELY, the reference set is close to a
fingerprint of the manuscript: the set of works a paper cites, queried together in one burst, is
often enough to identify the paper. For a confidential manuscript under review that is a real
consideration -- not a blocker, but it is your choice to make knowingly, so the command states it
every run. `--offline` performs the parse and reports UNCHECKABLE for everything, if you want to
see the structure without any network call.

=== VERDICTS ===

  ok            identifier resolves AND the metadata agrees
  MISMATCH      identifier resolves but returns a DIFFERENT paper   <- the 2026-08-14 case
  UNRESOLVABLE  identifier 404s; the cited paper may not exist
  DRIFT         right paper, but the title is paraphrased or the author list truncated
  UNCHECKABLE   no DOI and no arXiv id, so nothing can verify it

UNCHECKABLE is reported, never passed. 36% of our own library carries no identifier, so it will be
common in other people's bibliographies too; treating "no DOI" as "fine" is precisely the state
that made our own library's soundness unknowable.

The comparator is `verify.compare`, unchanged and shared with the library audit, so an improvement
to one improves both. A second comparator would drift from the first and then the two would
disagree about the same reference with no way to say which was right.
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verify as V

DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+")
ARXIV_RE = re.compile(r"arxiv[:\s]*((?:\d{4}\.\d{4,5})|(?:[a-z-]+(?:\.[A-Z]{2})?/\d{7}))", re.I)
YEAR_RE = re.compile(r"\b((?:18|19|20)\d{2})\b")


# ---------------------------------------------------------------- parsing

def parse_bib(path):
    """BibTeX entries. Reuses the library's own parser so the two agree on what an entry is."""
    from import_bib import parse_bib as _pb
    out = []
    for r in _pb(path):
        try:
            y = int(r.get("year", "") or 0) or None
        except ValueError:
            y = None
        out.append({
            "key": r.get("_key") or "?",
            "title": (r.get("title") or "").strip(),
            "authors": r.get("author") or "",
            "year": y,
            "journal": r.get("journal") or "",
            "volume": r.get("volume") or "",
            "pages": r.get("pages") or "",
            "doi": r.get("doi") or "",
            "arxiv": r.get("eprint") or "",
            "raw": "",
        })
    return out


def parse_tex(path):
    r"""\bibitem blocks from a .tex.

    A \bibitem has no field structure -- author, title and journal run together as prose -- so
    there is no title to compare. Verify by CONTAINMENT instead: pull the DOI/arXiv id out of the
    text, ask the authority what that identifier is, then check whether the authority's first
    author and year actually appear in the block. If the DOI resolves to Askar 2017 while the
    block says 'Reindl' and '2017', that is a mismatch, and no title parsing was needed."""
    text = open(path, encoding="utf-8", errors="replace").read()
    text = re.sub(r"(?m)^\s*%.*$", "", text)               # drop comments
    out = []
    parts = re.split(r"\\bibitem", text)[1:]
    for p in parts:
        p = p.split(r"\end{thebibliography}")[0]
        m = re.match(r"\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}", p)
        key = m.group(1).strip() if m else "?"
        body = p[m.end():] if m else p
        body = body.strip()
        doi = DOI_RE.search(body)
        arx = ARXIV_RE.search(body)
        years = YEAR_RE.findall(body)
        out.append({
            "key": key,
            "title": "",                                    # not extractable from prose
            "authors": "",
            "year": int(years[-1]) if years else None,
            "journal": "", "volume": "", "pages": "",
            "doi": doi.group(0).rstrip(".,;") if doi else "",
            "arxiv": arx.group(1) if arx else "",
            "raw": " ".join(body.split())[:600],
        })
    return out


def parse(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".bib", ".bibtex"):
        return parse_bib(path), "bib"
    if ext in (".tex", ".bbl"):
        return parse_tex(path), "tex"
    # fall back on content sniffing
    head = open(path, encoding="utf-8", errors="replace").read(4000)
    if re.search(r"@\s*[a-zA-Z]+\s*\{", head):
        return parse_bib(path), "bib"
    return parse_tex(path), "tex"


# ---------------------------------------------------------------- checking

def classify(ref, meta):
    """Map verify.compare's field findings onto the referee-facing verdicts.

    The split that matters: MISMATCH means a DIFFERENT PAPER, DRIFT means the right paper
    described sloppily. Measured on a real manuscript (paper_gammas.bib, 71 refs) and on the
    library: of 134 flagged records, 118 differed only in the title while author and year agreed
    -- section prefixes and preprint retitles, i.e. drift, not wrong references. Collapsing the
    two would bury the one real defect under a hundred cosmetic ones."""
    verdict, hard, soft, ratio = V.compare(
        {"title": ref["title"], "authors": ref["authors"], "year": ref["year"],
         "journal": ref["journal"], "volume": ref["volume"], "pages": ref["pages"]}, meta)
    findings = list(hard) + list(soft)
    different_paper = any(
        h.startswith("first-author:") or h.startswith("year:") or h.startswith("title:different")
        for h in hard)
    if different_paper:
        return "MISMATCH", findings, ratio
    drift = any(h.startswith("title:paraphrased") for h in hard) or \
        any(s.startswith("authors:truncated") for s in soft)
    if drift:
        return "DRIFT", findings, ratio
    return "ok", findings, ratio


def classify_tex(ref, meta):
    """Containment check for \\bibitem prose: does the authority's first author and year appear?"""
    body = V.fold(ref["raw"] or "").lower()
    findings = []
    sur = (meta.get("surnames") or [""])[0]
    if sur:
        key = V.surname_key(sur)
        present = key and key in re.sub(r"[^a-z]", "", V.fold(body))
        if not present:
            findings.append("first author %s does not appear in the entry" % sur)
    ay = meta.get("year")
    if ay and ref.get("year") and abs(int(ay) - int(ref["year"])) > 1:
        findings.append("entry year %s vs authority %s" % (ref["year"], ay))
    if findings:
        return "MISMATCH", findings, 0.0
    return "ok", findings, 1.0


def check_one(ref, mode, offline=False):
    out = dict(ref)
    doi = V.norm_doi(ref.get("doi"))
    arx = V.norm_arxiv(ref.get("arxiv")) or (V.doi_is_arxiv(doi) or "")
    if V.doi_is_arxiv(doi):
        doi = ""
    if not doi and not arx:
        out.update(verdict="UNCHECKABLE", findings=["no DOI and no arXiv id"],
                   authority="", resolved="")
        return out
    if offline:
        out.update(verdict="UNCHECKABLE", findings=["--offline: no network check performed"],
                   authority="", resolved="")
        return out

    if doi:
        authority, ident = "crossref", doi
        status, meta = V.fetch_crossref(doi)
    else:
        authority, ident = "arxiv", arx
        status, meta = V.fetch_arxiv_batch([arx]).get(arx, ("error:missing", None))

    out["authority"] = authority
    out["identifier"] = ident
    if status == "notfound":
        out.update(verdict="UNRESOLVABLE", findings=["%s does not resolve at %s" % (ident, authority)],
                   resolved="")
        return out
    if status != "ok" or not meta:
        out.update(verdict="UNCHECKABLE",
                   findings=["could not reach %s (%s) — retry before concluding anything"
                             % (authority, status)], resolved="")
        return out

    out["resolved"] = meta["title"]
    out["resolved_author"] = (meta.get("surnames") or [""])[0]
    out["resolved_year"] = meta.get("year")
    verdict, findings, ratio = (classify_tex(ref, meta) if mode == "tex"
                                else classify(ref, meta))
    out.update(verdict=verdict, findings=findings, ratio=ratio)
    return out


# ---------------------------------------------------------------- reporting

ORDER = ["MISMATCH", "UNRESOLVABLE", "DRIFT", "UNCHECKABLE", "ok"]


def report(results, path, mode):
    counts = {v: 0 for v in ORDER}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    print("\nchecked %d reference(s) in %s (%s)" % (len(results), os.path.basename(path), mode))
    print("  " + "   ".join("%s=%d" % (v, counts[v]) for v in ORDER if counts.get(v)))

    for level in ("MISMATCH", "UNRESOLVABLE"):
        rows = [r for r in results if r["verdict"] == level]
        if not rows:
            continue
        print("\n" + "=" * 76)
        print({"MISMATCH": "MISMATCH — the identifier resolves to a DIFFERENT paper",
               "UNRESOLVABLE": "UNRESOLVABLE — the identifier does not resolve"}[level])
        print("=" * 76)
        for r in rows:
            print("\n[%s]" % r["key"])
            if r.get("title"):
                print("   cited as   : %s" % r["title"][:92])
            elif r.get("raw"):
                print("   entry text : %s" % r["raw"][:92])
            print("   identifier : %s" % (r.get("identifier") or r.get("doi") or "—"))
            if r.get("resolved"):
                print("   RESOLVES TO: %s" % r["resolved"][:92])
                print("                %s %s" % (r.get("resolved_author") or "?",
                                                 r.get("resolved_year") or "?"))
            for f in r["findings"]:
                print("   • %s" % f[:96])

    drift = [r for r in results if r["verdict"] == "DRIFT"]
    if drift:
        print("\n" + "-" * 76)
        print("DRIFT — right paper, sloppy metadata (paraphrased title / truncated authors)")
        print("-" * 76)
        for r in drift:
            print("  [%-24s] %s" % (r["key"][:24], "; ".join(r["findings"])[:74]))

    unch = [r for r in results if r["verdict"] == "UNCHECKABLE"]
    if unch:
        print("\n" + "-" * 76)
        print("UNCHECKABLE — no identifier, so nothing can verify these")
        print("-" * 76)
        for r in unch:
            print("  [%-24s] %s" % (r["key"][:24], (r.get("title") or r.get("raw") or "")[:70]))

    bad = [r for r in results if r["verdict"] in ("MISMATCH", "UNRESOLVABLE")]
    if bad:
        print("\n" + "=" * 76)
        print("FOR A REFEREE REPORT — the useful sentence names what the DOI actually returns:")
        print("=" * 76)
        for r in bad:
            if r.get("resolved"):
                print('  "%s" cites DOI %s, which resolves to %s (%s %s) — a different paper.'
                      % (r["key"], r.get("identifier"), r["resolved"][:60],
                         r.get("resolved_author") or "?", r.get("resolved_year") or "?"))
            else:
                print('  "%s" cites %s, which does not resolve.'
                      % (r["key"], r.get("identifier") or "—"))
    return counts


# ---------------------------------------------------------------- entry point

def run(path, as_json=False, strict=False, offline=False, workers=6):
    if not os.path.exists(path):
        sys.exit("no such file: %s" % path)
    refs, mode = parse(path)
    if not refs:
        sys.exit("no references found in %s" % path)

    if not as_json and not offline:
        print("note: this queries Crossref/ADS with the document's identifiers. Individually they "
              "are public;\n      collectively a reference set can fingerprint a manuscript. "
              "Use --offline to skip all network.", file=sys.stderr)

    import concurrent.futures
    results = [None] * len(refs)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(check_one, r, mode, offline): i for i, r in enumerate(refs)}
        for f in concurrent.futures.as_completed(futs):
            i = futs[f]
            try:
                results[i] = f.result()
            except Exception as e:
                results[i] = dict(refs[i], verdict="UNCHECKABLE",
                                  findings=["checker error: %s" % type(e).__name__],
                                  authority="", resolved="")

    if as_json:
        print(json.dumps(results, indent=1, default=str))
        counts = {v: sum(1 for r in results if r["verdict"] == v) for v in ORDER}
    else:
        counts = report(results, path, mode)

    fail = counts.get("MISMATCH", 0) + counts.get("UNRESOLVABLE", 0)
    if strict:
        fail += counts.get("DRIFT", 0) + counts.get("UNCHECKABLE", 0)
    return 1 if fail else 0


# ---------------------------------------------------------------- selftest

def selftest():
    """Negative controls. The 2026-08-14 pairs are the real thing: the wrong DOIs RESOLVE, so a
    checker that only asks 'does it resolve' passes them and is worthless."""
    cases = []

    def eq(label, got, want):
        cases.append((label, got == want, "got=%r want=%r" % (got, want)))

    stingray = {"title": "Breaking news from the HST: the central star of the Stingray Nebula "
                         "is now returning towards the AGB",
                "authors": "Reindl, N. and Schaffenroth, V.", "year": 2017,
                "journal": "MNRAS Letters", "volume": "", "pages": "", "raw": ""}

    r = check_one(dict(stingray, key="good", doi="10.1093/mnrasl/slw175"), "bib")
    eq("correct DOI -> ok/DRIFT", r["verdict"] in ("ok", "DRIFT"), True)
    r = check_one(dict(stingray, key="wrongdoi", doi="10.1093/mnrasl/slw177"), "bib")
    eq("wrong DOI -> MISMATCH", r["verdict"], "MISMATCH")
    eq("  and it names the real paper", "MOCCA" in (r.get("resolved") or ""), True)
    r = check_one(dict(stingray, key="dead", doi="10.1093/mnras/stz99999999"), "bib")
    eq("nonexistent DOI -> UNRESOLVABLE", r["verdict"], "UNRESOLVABLE")
    r = check_one(dict(stingray, key="noid", doi="", arxiv=""), "bib")
    eq("no identifier -> UNCHECKABLE", r["verdict"], "UNCHECKABLE")
    r = check_one(dict(stingray, key="para", doi="10.1093/mnrasl/slw175",
                       title="News from Hubble: the Stingray Nebula central star heads to the AGB"),
                  "bib")
    eq("paraphrased title -> MISMATCH or DRIFT", r["verdict"] in ("MISMATCH", "DRIFT"), True)
    r = check_one(dict(stingray, key="off", doi="10.1093/mnrasl/slw175"), "bib", offline=True)
    eq("--offline -> UNCHECKABLE, no network", r["verdict"], "UNCHECKABLE")

    # \bibitem containment: the authority's author must appear in the prose.
    tex_ok = {"key": "b1", "title": "", "authors": "", "year": 2017, "journal": "", "volume": "",
              "pages": "", "doi": "10.1093/mnrasl/slw175", "arxiv": "",
              "raw": "N. Reindl, V. Schaffenroth et al., MNRAS 464, L51 (2017)."}
    eq("bibitem, right author -> ok", check_one(tex_ok, "tex")["verdict"], "ok")
    tex_bad = dict(tex_ok, doi="10.1093/mnrasl/slw177")
    eq("bibitem, DOI of another paper -> MISMATCH",
       check_one(tex_bad, "tex")["verdict"], "MISMATCH")

    bad = sum(1 for _, ok, _ in cases if not ok)
    for label, ok, detail in cases:
        print("%s  %-40s %s" % ("PASS" if ok else "FAIL", label, "" if ok else detail))
    print("\n%d/%d passed" % (len(cases) - bad, len(cases)))
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(
        description="Verify references in a document you did not write. Read-only; never touches "
                    "the library.")
    ap.add_argument("file", nargs="?", help=".bib or .tex (\\bibitem) file")
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--strict", action="store_true",
                    help="also exit non-zero on DRIFT and UNCHECKABLE")
    ap.add_argument("--offline", action="store_true",
                    help="parse only; no network call (for confidential material)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.file:
        ap.error("a file is required")
    return run(a.file, a.as_json, a.strict, a.offline, a.workers)


if __name__ == "__main__":
    sys.exit(main())
