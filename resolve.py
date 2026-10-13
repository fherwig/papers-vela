#!/usr/bin/env python3
"""resolve — validate every record against a SOURCE HIERARCHY and adopt the best one.

Policy (Falk, 2026-08-14):

    1. Hierarchy:  journal (Crossref)  >  NASA ADS  >  arXiv
    2. A record that validates against AT LEAST ONE credible source STAYS.
    3. The entry is rebuilt from the HIGHEST-RANKING source that validates it.
    4. Where sources DISAGREE, the disagreement is recorded as provenance on the entry
       rather than silently resolved.
    5. A record that validates against NONE is a removal candidate.

This replaces "does the DOI check out?" with "is this a real paper, and who says so?", which is
what actually decides whether an entry may be cited. It matters most for the 1,163 records with
no DOI and no arXiv id: DOIs only became routine around 1998, but ADS indexes astronomy back to
the 19th century, so most pre-DOI papers are verifiable -- just not by DOI.

VALIDATION means the source confirms THIS paper:
    title similarity >= 0.90  AND  first author agrees  AND  year within 1
Weaker than that is not validation. Nothing here is ever generated: every adopted field is
copied from a source response, or the record is left alone and reported.

  python3 resolve.py --sample 80              # measure, write nothing
  python3 resolve.py --all --out proposal.json
  python3 resolve.py --apply proposal.json    # backs library.db up first
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verify as V

RANK = {"journal": 0, "ads": 1, "arxiv": 2}          # lower wins
ADS_API = "https://api.adsabs.harvard.edu/v1/search/query"
ADS_FL = "bibcode,title,author,year,doi,pub,volume,page,identifier"


def ads_token():
    try:
        return open(os.path.expanduser("~/.config/ads/token")).read().strip()
    except OSError:
        return None


# Solr treats these as operators even inside some phrase contexts; an unescaped '[' from a title
# like 'late-type [WC] CSPNe' makes ADS return ZERO results, which reads as "no source has this
# paper" and would condemn a real record. Measured 2026-08-14 on library id 133.
SOLR_SPECIAL = str.maketrans({c: " " for c in '+-&|!(){}[]^"~*?:\\/'})


def ads_title_query(title):
    return " ".join((title or "").translate(SOLR_SPECIAL).split())


# Solr reads bare and/or/not as BOOLEAN OPERATORS in an unquoted title:(...) clause. A title
# containing 'and/or' cleans to '... Supernovae and or Neutron ...', which is a malformed
# expression and returns HTTP 400 -- treated (correctly) as "source unavailable", so the record
# sat in RECHECK forever and could never resolve. Measured 2026-08-14 on library id 896.
BOOLEAN_WORDS = {"and", "or", "not"}


def ads_terms_query(title):
    """Cleaned title safe for the UNQUOTED title:(...) form."""
    return " ".join(w for w in ads_title_query(title).split()
                    if w.lower() not in BOOLEAN_WORDS)


class SourceUnavailable(Exception):
    """The source could not be ASKED. Distinct from the source having no such paper.

    This distinction is the difference between a safe and an unsafe campaign: ADS allows 5,000
    queries a day, and if a quota rejection were reported the same way as "no match", every
    record checked after the quota ran out would look like it had been checked and found
    wanting -- and under this policy that means DELETE. A rate limit must never be able to
    masquerade as a verdict."""


def _ads_call(q, tok, rows=5):
    """-> list of docs (possibly empty). Raises SourceUnavailable if ADS could not be asked."""
    url = "%s?q=%s&fl=%s&rows=%d" % (ADS_API, urllib.parse.quote(q), ADS_FL, rows)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok})
    last = "unknown"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())["response"]["docs"]
        except urllib.error.HTTPError as e:
            last = "HTTP %d" % e.code
            if e.code in (429, 403):                      # quota / auth -> never a verdict
                raise SourceUnavailable("ADS quota or auth: %s" % last)
            if attempt == 2:
                raise SourceUnavailable("ADS %s" % last)
        except Exception as e:
            last = type(e).__name__
            if attempt == 2:
                raise SourceUnavailable("ADS %s" % last)
        time.sleep(1.5 * (attempt + 1))
    raise SourceUnavailable("ADS %s" % last)


def ads_lookup(rec, tok):
    """Search ADS by identifier AND by title, then keep the best candidate.

    Both routes always run, because the identifier is exactly what may be wrong. Library id 601
    (Khokhlov 1991) carries a DOI that resolves to Jordan 2012: an identifier-first lookup that
    stops on success returns the WRONG paper and the record looks unverifiable. Searching the
    title as well finds the real Khokhlov paper, which both validates the record AND supplies
    the correct identifier to adopt -- repairing the very defect this campaign is about."""
    if not tok:
        return None
    docs = []
    doi = V.norm_doi(rec.get("doi"))
    arx = V.norm_arxiv(rec.get("arxiv")) or (V.doi_is_arxiv(doi) or "")
    if doi and not V.doi_is_arxiv(doi):
        docs += _ads_call('doi:"%s"' % doi, tok) or []
    if arx:
        docs += _ads_call('arxiv:"%s"' % arx, tok) or []
    clean = ads_title_query(rec.get("title"))[:300]
    if clean:
        docs += _ads_call('title:"%s"' % clean, tok) or []
    if clean and not any(
            V.title_ratio(rec.get("title") or "", (d.get("title") or [""])[0]) >= 0.90
            for d in docs):
        # Unquoted fallback. An exact phrase misses when the stored title carries a TYPO --
        # library id 234 has 'Sajurai' for 'Sakurai' -- so fall back to keyword matching and
        # let title_ratio judge the candidates. Measured 2026-08-14.
        docs += _ads_call("title:(%s)" % ads_terms_query(rec.get("title")), tok, rows=8) or []
    if not docs:
        return None
    # Prefer a candidate that actually validates; fall back to the closest title.
    best, best_r = None, -1.0
    for d in docs:
        t = (d.get("title") or [""])[0]
        r = V.title_ratio(rec.get("title") or "", t)
        if r > best_r:
            best, best_r = d, r
    if not best:
        return None
    doi_v = best.get("doi")
    if isinstance(doi_v, list):
        doi_v = doi_v[0] if doi_v else None
    return {
        "title": (best.get("title") or [""])[0],
        "authors_full": " and ".join(best.get("author") or []),
        "surnames": [V.norm_surname((a or "").split(",")[0]) for a in (best.get("author") or [])],
        "n_authors": len(best.get("author") or []),
        "year": int(best["year"]) if str(best.get("year", "")).isdigit() else None,
        "journal": best.get("pub") or "",
        "volume": best.get("volume") or "",
        "pages": (best.get("page") or [""])[0] if isinstance(best.get("page"), list)
                 else (best.get("page") or ""),
        "doi": doi_v, "bibcode": best.get("bibcode"), "type": "ads",
    }


def mangled_title(t):
    """papers3 imported some titles straight out of PDF text layers, which inserts spaces inside
    words: 'Typ e Ia sup erno v ae as sites of p -process'. ADS cannot match that, so the record
    looks unverifiable and would be deleted -- but it is a real paper. Detect the signature by
    the share of very short tokens."""
    toks = [w for w in re.findall(r"[A-Za-z]+", t or "")]
    if len(toks) < 6:
        return False
    short = sum(1 for w in toks if len(w) <= 2)
    return short / len(toks) > 0.28


def nospace_ratio(a, b):
    """Compare titles with all spacing removed. Only used for the mangled-title rescue: it is
    deliberately blind to word boundaries, which is right when one side's boundaries are noise."""
    import difflib as _d
    x = re.sub(r"[^a-z0-9]", "", V.norm_title(a or ""))
    y = re.sub(r"[^a-z0-9]", "", V.norm_title(b or ""))
    if not x or not y:
        return 0.0
    return _d.SequenceMatcher(None, x, y).ratio()


def ads_by_author_year(rec, tok):
    """Last-resort ADS lookup: search by AUTHOR + YEAR, then rank by title similarity.

    ADS's title search ANDs every term, so ONE typo in a stored title returns zero results and
    the record looks like nothing anywhere has it. Library id 102 holds 'Abundances of the
    elements: Meteoric and solar' -- the real title is 'Meteoritic' -- and that single letter
    made Anders & Grevesse (1989), one of the most-cited papers in astrophysics, a deletion
    candidate. Searching the author and year instead and comparing titles afterwards finds it.
    Also covers PDF-mangled titles, where word boundaries are noise, via nospace_ratio."""
    lib = V.surnames(rec.get("authors"))
    if not lib or not rec.get("year"):
        return None
    # year RANGE, not an exact year: validates() already tolerates +/-1 because a record often
    # carries the preprint year while the authority carries the print year. Pinning the year
    # exactly here contradicted that and condemned real papers -- library id 896 stores 2015 for
    # a paper ADS dates 2016. Measured 2026-08-14.
    y = int(rec["year"])
    q = 'author:"%s" year:%d-%d' % (lib[0].replace('"', ""), y - 1, y + 1)
    docs = _ads_call(q, tok, rows=40)
    mang = mangled_title(rec.get("title"))
    best, best_r = None, 0.0
    for d in docs or []:
        t = (d.get("title") or [""])[0]
        r = V.title_ratio(rec.get("title"), t)
        if mang:
            r = max(r, nospace_ratio(rec.get("title"), t))
        if r > best_r:
            best, best_r = d, r
    if not best or best_r < 0.92:
        return None
    doi_v = best.get("doi")
    if isinstance(doi_v, list):
        doi_v = doi_v[0] if doi_v else None
    return {"title": (best.get("title") or [""])[0],
            "authors_full": " and ".join(best.get("author") or []),
            "surnames": [V.norm_surname((a or "").split(",")[0]) for a in (best.get("author") or [])],
            "n_authors": len(best.get("author") or []),
            "year": int(best["year"]) if str(best.get("year", "")).isdigit() else None,
            "journal": best.get("pub") or "", "volume": best.get("volume") or "",
            "pages": (best.get("page") or [""])[0] if isinstance(best.get("page"), list)
                     else (best.get("page") or ""),
            "doi": doi_v, "bibcode": best.get("bibcode"), "type": "ads",
            "_rescued": "%.2f" % best_r}


def validates(rec, meta):
    """Does this source confirm THIS paper?

    TWO ROUTES, because our own stored data is sometimes the corrupt side:

      strong  title >= 0.90  AND first author agrees AND year within 1
      exact   title >= 0.98  AND year exact          -- accepted even when the author check
              cannot corroborate, and flagged so provenance records why

    The second route exists because papers3 imports mangled non-ASCII author fields: library
    id 420 stores 'D Sch onberner' for Schönberner, so the author test fails against a
    perfectly good ADS record. Removing a real paper because OUR copy of the author name is
    broken is the wrong outcome -- and rebuilding from the source is exactly what repairs it.
    A near-exact title plus an exact year is strong evidence of identity; the Khokhlov
    wrong-DOI record (title 0.42) fails both routes, so the wrong-paper guard is intact.
    """
    if not meta or not meta.get("title"):
        return False, "no metadata"
    r = V.title_ratio(rec.get("title") or "", meta["title"])
    if r < 0.90 and meta.get("_rescued"):
        r = float(meta["_rescued"])          # mangled-title rescue: matched ignoring spacing
    if r < 0.90:
        return False, "title %.2f" % r
    ly, ay = rec.get("year"), meta.get("year")
    if not ly:
        return False, "record has no year to corroborate"
    if ay and abs(int(ly) - int(ay)) > 1:
        return False, "year %s vs %s" % (ly, ay)

    lib = V.surnames(rec.get("authors"))
    auth = meta.get("surnames") or []
    author_ok = False
    if lib and auth:
        k = V.surname_key(lib[0])
        author_ok = any(k == V.surname_key(s) or k.endswith(V.surname_key(s))
                        or V.surname_key(s).endswith(k) for s in auth if s)
    if author_ok:
        return True, "title %.2f + author" % r
    if r >= V.TITLE_CLEAN and ay and int(ly) == int(ay):
        why = "author field could not corroborate (stored authors may be corrupt)"
        return True, "title %.2f + exact year; %s" % (r, why)
    if not lib:
        return False, "record has no authors to corroborate"
    return False, "first author %s absent" % lib[0]


def gather(rec, tok, deep=False):
    """Query the hierarchy. -> {name: (meta, ok, why)}

    STOPS at the top tier that validates, unless deep=True. NASA ADS allows 5,000 queries a day
    and this library holds 3,214 records needing up to three calls each; exhausting the quota
    would make every later record return "no source had it", which under this policy reads as
    evidence to DELETE it. A rate limit must never be able to look like a verdict. Records that
    the journal tier already validates therefore do not consume ADS budget -- the hierarchy says
    journal wins anyway. Use --deep to query every tier and get full cross-source disagreement
    notes when the budget allows."""
    out = {}
    doi = V.norm_doi(rec.get("doi"))
    arxdoi = V.doi_is_arxiv(doi)
    arx = V.norm_arxiv(rec.get("arxiv")) or (arxdoi or "")

    if doi and not arxdoi:
        st, m = V.fetch_crossref(doi)
        if st == "ok" and m:
            ok, why = validates(rec, m)
            out["journal"] = (m, ok, why)
        else:
            out["journal"] = (None, False, st)

    if deep or not out.get("journal", (None, False, ""))[1]:
        try:
            m = ads_lookup(rec, tok)
            if m:
                ok, why = validates(rec, m)
                out["ads"] = (m, ok, why)
            else:
                m2 = ads_by_author_year(rec, tok)
                if m2:
                    ok, why = validates(rec, m2)
                    out["ads"] = (m2, ok, "author+year rescue; " + why)
                else:
                    out["ads"] = (None, False, "no match")
        except SourceUnavailable as e:
            out["ads"] = (None, False, "UNAVAILABLE: %s" % e)

    if arx and (deep or not any(v[1] for v in out.values())):
        got = V.fetch_arxiv_batch([arx])
        st, m = got.get(arx, ("error", None))
        if st == "ok" and m:
            ok, why = validates(rec, m)
            out["arxiv"] = (m, ok, why)
        else:
            out["arxiv"] = (None, False, st)
    return out


def disagreements(sources):
    """Note where VALIDATING sources differ, so provenance records it instead of hiding it."""
    notes = []
    val = {k: v[0] for k, v in sources.items() if v[1] and v[0]}
    if len(val) < 2:
        return notes
    names = sorted(val, key=lambda k: RANK[k])
    top = val[names[0]]
    for other in names[1:]:
        o = val[other]
        r = V.title_ratio(top["title"], o["title"])
        if r < V.TITLE_CLEAN:
            notes.append("%s title differs from %s (%.2f): %r"
                         % (other, names[0], r, o["title"][:70]))
        if top.get("year") and o.get("year") and int(top["year"]) != int(o["year"]):
            notes.append("%s year %s vs %s %s" % (other, o["year"], names[0], top["year"]))
    return notes


def decide(rec, sources):
    """-> dict describing the outcome for this record."""
    validating = sorted([k for k, v in sources.items() if v[1]], key=lambda k: RANK[k])
    out = {"paper_id": rec["id"], "citekey": rec["citekey"], "lib_title": rec["title"],
           "checked": {k: {"ok": v[1], "why": v[2]} for k, v in sources.items()}}
    if not validating:
        # A source that could not be ASKED is not evidence of anything. If any tier was
        # unavailable (ADS quota, network), this record is RECHECK -- never REMOVE. Deleting on
        # an unanswered question is how a rate limit turns into data loss.
        unavailable = [k for k, v in sources.items() if str(v[2]).startswith("UNAVAILABLE")]
        if unavailable or not sources:
            out["outcome"] = "RECHECK"
            out["reason"] = ("; ".join("%s: %s" % (k, v[2]) for k, v in sources.items())
                             or "no source was reachable")
            return out
        out["outcome"] = "REMOVE"
        out["reason"] = "; ".join("%s: %s" % (k, v[2]) for k, v in sources.items())
        return out
    win = validating[0]
    meta = sources[win][0]
    out["outcome"] = "KEEP"
    out["authority"] = win
    out["provenance"] = "Verified against %s %s (%s)" % (
        win, time.strftime("%Y-%m-%d"), sources[win][2])
    if meta.get("bibcode"):
        out["provenance"] += "; ADS bibcode %s" % meta["bibcode"]
    notes = disagreements(sources)
    if notes:
        out["provenance"] += "; DISAGREEMENT: " + " | ".join(notes)
    out["disagreements"] = notes
    # Fields to adopt from the winning source. Only non-empty values, never generated.
    fields = {}
    af = meta.get("authors_full")
    if af and af.strip() != (rec.get("authors") or "").strip():
        fields["authors"] = af          # repairs papers3's mangled non-ASCII author fields
    for k in ("title", "journal", "volume", "pages", "year"):
        v = meta.get(k)
        if v not in (None, "", []) and str(v).strip() != str(rec.get(k) or "").strip():
            fields[k] = v
    # THE STALE-IDENTIFIER RULE. If the winning source validates this paper but the DOI we hold
    # is not the DOI it names, our DOI points somewhere else and must not survive on a record
    # that now reads as verified. Library id 601 is the case: ADS confirms Khokhlov 1991 by
    # title, has no DOI for it (pre-DOI A&A), while the record carries a DOI resolving to
    # Jordan 2012. Adopting the title and leaving that DOI in place would manufacture exactly
    # the defect this campaign exists to remove.
    src_doi = V.norm_doi(meta.get("doi") or "")
    our_doi = V.norm_doi(rec.get("doi") or "")
    if src_doi and src_doi != our_doi:
        fields["doi"] = meta["doi"]
    elif our_doi and not src_doi and not sources.get("journal", (None, False, ""))[1]:
        # we hold a DOI, the winning source names none, and Crossref did NOT validate it
        fields["doi"] = ""
        out["cleared_doi"] = rec.get("doi")
        out["provenance_extra"] = ("stored DOI %s did not validate against any source and the "
                                   "validating source names no DOI; cleared" % rec.get("doi"))
    if meta.get("bibcode"):
        out["bibcode"] = meta["bibcode"]
    if out.get("provenance_extra"):
        out["provenance"] += "; " + out["provenance_extra"]
    out["adopt"] = fields
    return out


def load(libdb, only_ids=None):
    c = sqlite3.connect("file:%s?mode=ro" % libdb, uri=True)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        "SELECT id,citekey,title,authors,year,journal,volume,pages,doi,arxiv,source "
        "FROM papers ORDER BY id")]
    c.close()
    return [r for r in rows if not only_ids or r["id"] in only_ids]


def apply_proposal(libdb, path, do_remove, quarantine_dir):
    """Apply a proposal. Backs up first; exports anything it removes BEFORE removing it.

    Removal is the only irreversible step in this campaign, so it is staged: every removed
    record is written out in full (with its list memberships) to a restore file first. If a
    removal turns out to be wrong -- a source outage, an ADS quota error, a title too mangled
    to match -- the record can be put back. Deleting with no restore path would make a
    verification bug permanent, which is the failure this whole effort is about."""
    import shutil, datetime
    proposal = json.load(open(path))
    bak = "%s.bak-resolve-%s" % (libdb, datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(libdb, bak)
    print("backed up -> %s" % bak)

    c = sqlite3.connect(libdb)
    c.row_factory = sqlite3.Row
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import gate
    gate.ensure_table(c)

    keep = [o for o in proposal if o["outcome"] == "KEEP"]
    rem = [o for o in proposal if o["outcome"] == "REMOVE"]

    updated = noted = 0
    for o in keep:
        pid = o["paper_id"]
        fields = o.get("adopt") or {}
        if fields:
            cols = ", ".join("%s=?" % k for k in fields)
            c.execute("UPDATE papers SET %s WHERE id=?" % cols,
                      list(fields.values()) + [pid])
            updated += 1
        # provenance lands in notes, so it travels into references.bib as note={...}
        prov = o.get("provenance") or ""
        if prov:
            cur = c.execute("SELECT notes FROM papers WHERE id=?", (pid,)).fetchone()
            old = (cur["notes"] or "") if cur else ""
            old = re.sub(r"\[provenance\][^\[]*", "", old).strip()
            c.execute("UPDATE papers SET notes=? WHERE id=?",
                      ((old + " " if old else "") + "[provenance] " + prov, pid))
            noted += 1
        gate.record(c, pid, "verified", o.get("authority", "?"),
                    o.get("bibcode") or "", 1.0, prov)
    c.commit()
    print("updated fields on %d record(s); wrote provenance on %d" % (updated, noted))

    if not rem:
        print("no removal candidates")
        c.close(); return 0
    os.makedirs(quarantine_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dump = os.path.join(quarantine_dir, "removed-%s.json" % stamp)
    payload = []
    for o in rem:
        row = c.execute("SELECT * FROM papers WHERE id=?", (o["paper_id"],)).fetchone()
        if not row:
            continue
        lists = [r["name"] for r in c.execute(
            "SELECT l.name FROM lists l JOIN paper_lists pl ON pl.list_id=l.id "
            "WHERE pl.paper_id=?", (o["paper_id"],))]
        payload.append({"record": dict(row), "lists": lists, "why": o.get("reason", ""),
                        "checked": o.get("checked", {})})
    json.dump(payload, open(dump, "w"), indent=1, default=str)
    print("exported %d removal candidate(s) -> %s" % (len(payload), dump))

    if not do_remove:
        print("NOT removed (no --remove given). Review the export, then re-run with --remove.")
        c.close(); return 0
    for o in rem:
        c.execute("DELETE FROM paper_lists WHERE paper_id=?", (o["paper_id"],))
        c.execute("DELETE FROM verification WHERE paper_id=?", (o["paper_id"],))
        c.execute("DELETE FROM papers WHERE id=?", (o["paper_id"],))
    c.commit()
    left = c.execute("SELECT COUNT(*) n FROM papers").fetchone()["n"]
    print("REMOVED %d record(s); library now %d papers" % (len(rem), left))
    print("restore file: %s" % dump)
    c.close()
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lib", default=None, help="library dir (default: the one papers.py resolves)")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--ids", help="file of paper ids, one per line: re-check just these")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--apply", metavar="PROPOSAL.JSON",
                    help="apply a proposal: adopt fields + write provenance (backs up first)")
    ap.add_argument("--remove", action="store_true",
                    help="with --apply, also DELETE records no source validated "
                         "(exported to a restore file first)")
    ap.add_argument("--quarantine", default=None,
                    help="where removal exports go (default <lib>/quarantine)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--deep", action="store_true",
                    help="query every tier even when a higher one validates "
                         "(full disagreement notes; uses far more ADS quota)")
    a = ap.parse_args()
    if not a.lib:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import papers
        a.lib = papers.LIB

    libdb = os.path.join(a.lib, "library.db")
    if a.apply:
        return apply_proposal(libdb, a.apply, a.remove,
                              a.quarantine or os.path.join(a.lib, "quarantine"))
    only = None
    if a.ids:
        only = {int(x) for x in open(a.ids).read().split() if x.strip().isdigit()}
    rows = load(libdb, only)
    if a.sample:
        step = max(1, len(rows) // a.sample)
        rows = rows[::step][:a.sample]
    tok = ads_token()
    if not tok:
        print("WARNING: no ADS token — the middle tier of the hierarchy is unavailable, so this "
              "run will over-report REMOVE. Fix ~/.config/ads/token before trusting it.")
    print("resolving %d record(s) through journal > ads > arxiv ..." % len(rows), flush=True)

    import concurrent.futures
    # Checkpoint as we go. A full pass is thousands of network calls against a source with a
    # DAILY quota, so it must survive being stopped and resumed tomorrow rather than losing
    # everything at 90%.
    ckpt = (a.out or "proposal.json") + ".partial.jsonl"
    seen = set()
    if os.path.exists(ckpt):
        for line in open(ckpt):
            try:
                seen.add(json.loads(line)["paper_id"])
            except Exception:
                pass
        print("resuming: %d record(s) already decided in %s" % (len(seen), ckpt))
    rows = [r for r in rows if r["id"] not in seen]
    prior = []
    if os.path.exists(ckpt):
        for line in open(ckpt):
            try:
                prior.append(json.loads(line))
            except Exception:
                pass
    fh = open(ckpt, "a")
    out, done = list(prior), 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(gather, r, tok, a.deep): r for r in rows}
        for f in concurrent.futures.as_completed(futs):
            r = futs[f]
            try:
                sources = f.result()
            except Exception as e:
                sources = {}
            d = decide(r, sources)
            out.append(d)
            fh.write(json.dumps(d) + "\n"); fh.flush()
            done += 1
            if done % 25 == 0:
                print("  %d/%d" % (done, len(rows)), flush=True)

    fh.close()
    keep = [o for o in out if o["outcome"] == "KEEP"]
    rem = [o for o in out if o["outcome"] == "REMOVE"]
    recheck = [o for o in out if o["outcome"] == "RECHECK"]
    by_auth = {}
    for o in keep:
        by_auth[o["authority"]] = by_auth.get(o["authority"], 0) + 1
    dis = [o for o in keep if o.get("disagreements")]
    adopt = [o for o in keep if o.get("adopt")]

    n = len(out)
    print("\n=== RESOLUTION on %d record(s) ===" % n)
    print("  KEEP (validated by >=1 source)  %5d  (%.1f%%)" % (len(keep), 100.0*len(keep)/max(n,1)))
    for k in ("journal", "ads", "arxiv"):
        if by_auth.get(k):
            print("      via %-8s %5d" % (k, by_auth[k]))
    print("  REMOVE (every source asked, none validated) %5d  (%.1f%%)"
          % (len(rem), 100.0*len(rem)/max(n,1)))
    if recheck:
        print("  RECHECK (a source could not be asked)     %5d  <- NOT removal candidates;"
              " re-run to resolve" % len(recheck))
    print("\n  entries whose fields would be rebuilt from the winning source: %d" % len(adopt))
    print("  entries where sources DISAGREE (provenance note added):        %d" % len(dis))
    if a.out:
        json.dump(out, open(a.out, "w"), indent=1)
        print("\n  wrote %s — proposal only, library.db untouched" % a.out)
    for o in rem[:6]:
        print("\n  REMOVE [%s] %s\n     %s" % (o["paper_id"], (o["lib_title"] or "")[:66], o["reason"][:110]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
