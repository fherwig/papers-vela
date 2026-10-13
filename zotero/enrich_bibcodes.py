#!/usr/bin/env python3
"""
zotero_enrich_bibcodes.py — populate the papers-vela `bibcode` column from two sources:

  1. Zotero's own 'extra' field  (free — no API calls, reads "ADS Bibcode: …" lines)
  2. NASA ADS search API         (for papers whose Zotero entry has no bibcode)

Run this once after initial import, and again whenever you've added papers without
going through the ADS Zotero connector (e.g. from a BibTeX file).

Optionally writes found bibcodes back to Zotero's 'extra' field so Zotero stays in
sync. Pass --update-zotero for this; requires Zotero to be closed.

Usage:
  python3 zotero_enrich_bibcodes.py [--ads-only] [--zotero-only]
                                     [--update-zotero] [--dry-run]
                                     [--limit N] [--verbose]

Env vars (user-configured — no built-in defaults):
  PAPERS_LIB   papers-vela library dir
  PAPERS_VELA papers-vela repo dir
  ZOTERO_DB    zotero.sqlite path  (default ~/Zotero/zotero.sqlite)
"""

import argparse, json, os, re, sqlite3, subprocess, sys, time, unicodedata, urllib.parse, urllib.request

PAPERS_VELA = os.environ.get("PAPERS_VELA") or os.environ.get("PAPERS_AGENT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.expanduser(PAPERS_VELA))
from papers import ads_token, last_name, require_lib

LIB       = os.path.expanduser(os.environ.get("PAPERS_LIB") or "")   # REQUIRED, no default
DB        = os.path.join(LIB, "library.db")
ZOTERO_DB = os.path.expanduser(os.environ.get("ZOTERO_DB", "~/Zotero/zotero.sqlite"))

# Matches "ADS Bibcode: XXX" (Zotero connector) and bare "bibcode: XXX"
_BC_RE = re.compile(r"(?im)^(?:ads\s+)?bibcode:\s*(\S+)")


# ---------------------------------------------------------------------------
# Identifier helpers
# ---------------------------------------------------------------------------

def norm_doi(s):
    s = re.sub(r"^https?://(dx\.)?doi\.org/", "", (s or "").strip())
    return s if re.match(r"10\.\d{4}", s) else None


def extract_arxiv(arxiv_col, doi_col):
    _doi_re = re.compile(r"10\.48550/arxiv\.([^\s/?#]+)", re.IGNORECASE)
    _url_re = re.compile(r"arxiv\.org/(?:abs|pdf)/([^\s/?#]+)", re.IGNORECASE)
    if arxiv_col:
        return re.sub(r"v\d+$", "", arxiv_col.strip())
    for src in (doi_col or "",):
        m = _doi_re.search(src) or _url_re.search(src)
        if m:
            return re.sub(r"v\d+$", "", m.group(1))
    return None


def _title_key(s):
    """Normalised title for cross-store matching: accent- and punctuation-free."""
    t = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^\w\s]", " ", t).lower().split())


# ---------------------------------------------------------------------------
# Source 1: pull bibcodes already in Zotero's extra field
# ---------------------------------------------------------------------------

def harvest_from_zotero(pa, dry_run=False, verbose=False):
    """Read 'ADS Bibcode:' lines from Zotero's extra field; update papers-vela."""
    zc = sqlite3.connect(f"file:{ZOTERO_DB}?mode=ro&immutable=1", uri=True)
    zc.row_factory = sqlite3.Row

    rows = zc.execute("""
        SELECT i.key AS zkey, dv.value AS extra
        FROM itemData id
        JOIN itemDataValues dv ON dv.valueID = id.valueID
        JOIN fields f ON f.fieldID = id.fieldID AND f.fieldName = 'extra'
        JOIN items i ON i.itemID = id.itemID
        WHERE id.itemID NOT IN (SELECT itemID FROM deletedItems)
    """).fetchall()

    # Build DOI → bibcode and title → bibcode maps. Matching on DOI alone
    # silently skips every record that stores a URL or nothing in `doi`, so a
    # bibcode Zotero already holds stays invisible here and costs an ADS call
    # later — or is never found at all.
    doi_to_bc, title_to_bc = {}, {}
    for row in rows:
        m = _BC_RE.search(row["extra"])
        if not m:
            continue
        bc = m.group(1)
        fields = dict(zc.execute("""
            SELECT f.fieldName, dv.value FROM itemData id
            JOIN itemDataValues dv ON dv.valueID = id.valueID
            JOIN fields f ON f.fieldID = id.fieldID
            JOIN items i ON i.itemID = id.itemID
            WHERE i.key = ? AND f.fieldName IN ('DOI', 'title')
        """, (row["zkey"],)).fetchall())
        doi = norm_doi(fields.get("DOI"))
        if doi:
            doi_to_bc[doi] = bc
        if fields.get("title"):
            title_to_bc.setdefault(_title_key(fields["title"]), bc)

    zc.close()

    updated = 0
    for p in pa.execute(
            "SELECT id, doi, title FROM papers WHERE bibcode IS NULL OR bibcode = ''"):
        doi = norm_doi(p["doi"])
        bc = doi_to_bc.get(doi) if doi else None
        via = "zotero/doi"
        if not bc and p["title"]:
            bc = title_to_bc.get(_title_key(p["title"]))
            via = "zotero/title"
        if bc:
            if verbose:
                print(f"  [{via}] {bc}")
            if not dry_run:
                pa.execute("UPDATE papers SET bibcode=? WHERE id=?", (bc, p["id"]))
            updated += 1

    if not dry_run:
        pa.commit()
    print(f"Zotero extra fields: {updated} bibcodes harvested")
    return updated


# ---------------------------------------------------------------------------
# Source 2: ADS API lookup
# ---------------------------------------------------------------------------

def _ads_query(q, token, fl="bibcode", rows=1):
    UA = {"Authorization": f"Bearer {token}", "User-Agent": "papers-vela/1.0"}
    url = ("https://api.adsabs.harvard.edu/v1/search/query?"
           + urllib.parse.urlencode({"q": q, "fl": fl, "rows": rows}))
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r).get("response", {}).get("docs", [])
    except Exception:
        return []


def ads_lookup(doi=None, arxiv=None, token=None):
    if not token:
        return None
    for q in ([f"doi:{doi}"] if doi else []) + ([f"arxiv:{arxiv}"] if arxiv else []):
        docs = _ads_query(q, token)
        if docs:
            return docs[0].get("bibcode")
    return None


def _title_tokens(s):
    """Comparable word set: fold accents, drop math/markup and short words."""
    t = re.sub(r"\$[^$]*\$", " ", (s or ""))
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    return {w for w in re.findall(r"[a-z0-9]+", t.lower()) if len(w) > 2}


def ads_lookup_by_title(title, authors, year, token):
    """Last-resort lookup for records ADS can't match on DOI or arXiv ID.

    A title search can easily return the wrong paper, so the hit is only
    accepted when the returned record independently agrees on all three of
    title, first-author surname, and year. Returns None rather than a guess.
    """
    if not token or not title:
        return None

    want = _title_tokens(title)
    if len(want) < 3:                      # too generic to verify safely
        return None
    surname = last_name(authors) if authors else None

    yr = f" year:{int(year)-1}-{int(year)+1}" if year else ""
    au = f' author:"{surname}"' if surname and surname != "Anon" else ""
    queries = [f'title:"{title}"{au}{yr}']

    # Accents, hyphens and reaction notation ("(p,γ)") break ADS's phrase
    # parser. Retry on a stripped title, and without the author clause, since
    # particle surnames ("de Jager") don't match the bare surname either.
    clean = re.sub(r"[^\w\s]", " ",
                   unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode())
    clean = " ".join(clean.split())
    if clean and clean != title:
        queries.append(f'title:"{clean}"{yr}')

    docs = []
    for q in queries:
        docs = _ads_query(q, token, fl="bibcode,title,year,author,doctype", rows=5)
        if docs:
            break

    verified = []
    for doc in docs:
        got = doc.get("title") or []
        got_tokens = _title_tokens(got[0] if got else "")
        if not got_tokens:
            continue
        # Jaccard-style agreement on the smaller set, tolerant of subtitles.
        overlap = len(want & got_tokens) / min(len(want), len(got_tokens))
        if overlap < 0.85:
            continue
        if year and doc.get("year") and abs(int(doc["year"]) - int(year)) > 1:
            continue
        if surname and surname != "Anon":
            got_auth = doc.get("author") or []
            if not got_auth or last_name(got_auth[0]) != surname:
                continue
        verified.append(doc)

    if not verified:
        return None
    # Several records can describe the same work (meeting abstract, preprint,
    # version of record). Prefer the most citable; an abstract is a last resort.
    rank = {"article": 0, "eprint": 1, "inbook": 2, "book": 2, "phdthesis": 2,
            "software": 2, "dataset": 2, "inproceedings": 3, "abstract": 4}
    verified.sort(key=lambda d: rank.get(d.get("doctype"), 3))
    return verified[0].get("bibcode")


def harvest_from_ads(pa, limit=None, dry_run=False, verbose=False,
                     update_zotero=False, title_fallback=True):
    """Query ADS for each paper still missing a bibcode."""
    token = ads_token()
    if not token:
        print("No ADS token at ~/.config/ads/token — skipping ADS lookups")
        return 0

    # Records with neither DOI nor arXiv ID are included too: the title
    # fallback below can still resolve them.
    candidates = pa.execute("""
        SELECT id, doi, arxiv, title, authors, year FROM papers
        WHERE bibcode IS NULL OR bibcode = ''
    """).fetchall()

    total = len(candidates) if not limit else min(len(candidates), limit)
    print(f"ADS lookups: {total} papers to query...")

    # Optionally open Zotero for writing back
    zc = zc_field_id = None
    if update_zotero and not dry_run:
        if _zotero_is_running():
            print("  ⚠  --update-zotero requested but Zotero is running — skipping Zotero writes")
            update_zotero = False
        else:
            zc = sqlite3.connect(ZOTERO_DB)
            zc.row_factory = sqlite3.Row
            row = zc.execute(
                "SELECT fieldID FROM fields WHERE fieldName='extra'").fetchone()
            zc_field_id = row["fieldID"] if row else None

    found = missed = by_title = 0
    for i, row in enumerate(candidates[:total], 1):
        doi   = norm_doi(row["doi"])
        arxiv = extract_arxiv(row["arxiv"], row["doi"])
        bc    = ads_lookup(doi, arxiv, token)
        via   = "ADS"

        if not bc and title_fallback:
            bc = ads_lookup_by_title(row["title"], row["authors"], row["year"], token)
            if bc:
                via = "ADS/title"
                by_title += 1

        if bc:
            if verbose:
                print(f"  [{via}] {bc}")
            if not dry_run:
                pa.execute("UPDATE papers SET bibcode=? WHERE id=?", (bc, row["id"]))
                if zc and zc_field_id:
                    _write_zotero_bibcode(zc, row["id"], bc, zc_field_id, pa)
            found += 1
        else:
            missed += 1

        if i % 50 == 0 and not dry_run:
            pa.commit()
            print(f"  {i}/{total}  found={found}  no-match={missed}")

        time.sleep(0.2)

    if not dry_run:
        pa.commit()
    if zc:
        zc.commit(); zc.close()

    tail = f" ({by_title} via title fallback)" if by_title else ""
    print(f"ADS lookups: {found} found{tail}, {missed} no match")
    return found


# ---------------------------------------------------------------------------
# Optional Zotero write-back
# ---------------------------------------------------------------------------

def _zotero_is_running():
    try:
        return subprocess.run(["pgrep", "-i", "zotero"],
                              capture_output=True).returncode == 0
    except FileNotFoundError:
        return False


def _write_zotero_bibcode(zc, pa_id, bibcode, field_id, pa):
    """Write 'ADS Bibcode: X' into the Zotero item's extra field."""
    # Find Zotero item via DOI match
    p = pa.execute("SELECT doi FROM papers WHERE id=?", (pa_id,)).fetchone()
    if not p or not norm_doi(p["doi"]):
        return
    doi = norm_doi(p["doi"])
    zitem = zc.execute("""
        SELECT i.itemID FROM items i
        JOIN itemData id ON id.itemID = i.itemID
        JOIN itemDataValues dv ON dv.valueID = id.valueID
        JOIN fields f ON f.fieldID = id.fieldID AND f.fieldName = 'DOI'
        WHERE lower(dv.value) = ?
          AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
    """, (doi.lower(),)).fetchone()
    if not zitem:
        return

    item_id = zitem["itemID"]
    existing = zc.execute("""
        SELECT id.valueID, dv.value FROM itemData id
        JOIN itemDataValues dv ON dv.valueID = id.valueID
        WHERE id.itemID = ? AND id.fieldID = ?
    """, (item_id, field_id)).fetchone()

    bibcode_line = f"ADS Bibcode: {bibcode}"
    if existing:
        if _BC_RE.search(existing["value"]):
            return
        new_value = existing["value"].rstrip() + f"\n{bibcode_line}"
    else:
        new_value = bibcode_line

    zc.execute("INSERT OR IGNORE INTO itemDataValues(value) VALUES(?)", (new_value,))
    vid = zc.execute("SELECT valueID FROM itemDataValues WHERE value=?",
                     (new_value,)).fetchone()["valueID"]
    if existing:
        zc.execute("UPDATE itemData SET valueID=? WHERE itemID=? AND fieldID=?",
                   (vid, item_id, field_id))
    else:
        zc.execute("INSERT INTO itemData(itemID, fieldID, valueID) VALUES(?,?,?)",
                   (item_id, field_id, vid))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--zotero-only",   action="store_true",
                    help="only harvest from Zotero extra fields, skip ADS")
    ap.add_argument("--ads-only",      action="store_true",
                    help="skip Zotero harvest, go straight to ADS lookups")
    ap.add_argument("--update-zotero", action="store_true",
                    help="write found bibcodes back to Zotero extra field "
                         "(requires Zotero to be closed)")
    ap.add_argument("--no-title-fallback", action="store_true",
                    help="skip the verified title/author/year lookup used for "
                         "records ADS cannot match on DOI or arXiv ID")
    ap.add_argument("--limit",  type=int, default=None,
                    help="max number of ADS API calls to make")
    ap.add_argument("--dry-run",  action="store_true")
    ap.add_argument("--verbose",  action="store_true")
    a = ap.parse_args()
    require_lib()

    if not os.path.exists(DB):
        sys.exit(f"papers-vela library not found at {DB}")

    pa = sqlite3.connect(DB); pa.row_factory = sqlite3.Row

    # Ensure bibcode column exists
    try:
        pa.execute("ALTER TABLE papers ADD COLUMN bibcode TEXT"); pa.commit()
    except sqlite3.OperationalError:
        pass

    before = pa.execute("SELECT COUNT(bibcode) FROM papers").fetchone()[0]

    if not a.ads_only:
        harvest_from_zotero(pa, dry_run=a.dry_run, verbose=a.verbose)

    if not a.zotero_only:
        harvest_from_ads(pa, limit=a.limit, dry_run=a.dry_run,
                         verbose=a.verbose, update_zotero=a.update_zotero,
                         title_fallback=not a.no_title_fallback)

    after = pa.execute("SELECT COUNT(bibcode) FROM papers").fetchone()[0]
    total = pa.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    pa.close()

    added = after - before
    print(f"\nCoverage: {after}/{total} ({100*after//total}%)  "
          f"[+{added} this run]")


if __name__ == "__main__":
    main()
