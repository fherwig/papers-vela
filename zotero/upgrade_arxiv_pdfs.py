#!/usr/bin/env python3
"""
upgrade_arxiv_pdfs.py — replace arXiv preprint PDFs with journal versions.

Targets papers where:
  • The papers-vela DOI is 10.48550/arxiv.* (imported as a preprint)
  • The ADS bibcode points to a real journal record (not arXiv)
  • A PDF already exists (it's the arXiv version)

For each match it:
  1. Downloads the publisher PDF via ADS link_gateway / ADS esource / Unpaywall
  2. Deletes all existing PDF attachments from the Zotero item (files + DB)
  3. Converts the Zotero item from "preprint" to "journalArticle" type
  4. Updates all metadata fields (DOI, URL, journal, volume, issue, pages, date)
  5. Registers the new PDF as a Zotero attachment
  6. Updates papers-vela's pdf_path and bibliographic fields

⚠  REQUIRES ZOTERO TO BE CLOSED — writes directly to zotero.sqlite.

Usage:
  python3 upgrade_arxiv_pdfs.py [--dry-run] [--limit N] [--verbose]

Env vars: PAPERS_LIB, PAPERS_VELA, ZOTERO_DB, ZOTERO_DIR (same as fetch_pdfs.py)
"""

import argparse, datetime, os, re, sys, sqlite3

PAPERS_VELA = os.path.expanduser(os.environ.get("PAPERS_VELA") or os.environ.get("PAPERS_AGENT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PAPERS_VELA)

import importlib.util as _ilu

def _load(name, path):
    spec = _ilu.spec_from_file_location(name, path)
    mod  = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

_fp = _load("fetch_pdfs", os.path.join(PAPERS_VELA, "zotero", "fetch_pdfs.py"))

from papers import ads_token, ads_get, download, is_pdf, reindex_lists
import urllib.parse, urllib.request, json as _json

EMAIL = os.environ.get("PAPERS_EMAIL", "papers-vela@example.com")

LIB         = os.path.expanduser(os.environ.get("PAPERS_LIB") or next((p for p in ("~/Zotero/papers-vela", "~/Zotero/papers-agent")  # the pre-rename name still works
                if os.path.isdir(os.path.expanduser(p))), "~/Zotero/papers-vela"))
DB          = os.path.join(LIB, "library.db")
ZOTERO_DB   = os.path.expanduser(os.environ.get("ZOTERO_DB",  "~/Zotero/zotero.sqlite"))
ZOTERO_DIR  = os.path.expanduser(os.environ.get("ZOTERO_DIR", "~/Zotero"))
STORAGE_DIR = os.path.join(ZOTERO_DIR, "storage")

# Zotero item type IDs
_TYPE_JOURNAL  = 22
_TYPE_PREPRINT = 31

# Field IDs (from Zotero fieldsCombined)
_FLD = {
    "title":               1,
    "abstractNote":        2,
    "date":                6,
    "language":            7,
    "shortTitle":          8,
    "archive":             9,
    "archiveLocation":     10,
    "libraryCatalog":      11,
    "callNumber":          12,
    "url":                 13,
    "accessDate":          14,
    "rights":              15,
    "extra":               16,
    "volume":              19,
    "place":               21,
    "publisher":           23,
    "pages":               32,
    "publicationTitle":    38,
    "series":              41,
    "seriesTitle":         18,
    "seriesText":          77,
    "journalAbbreviation": 78,
    "issue":               76,
    "DOI":                 59,
    "ISSN":                79,
    "citationKey":         64,
    "extra":               16,
    # preprint-only (to remove on conversion)
    "repository":          61,
    "archiveID":           97,
    "genre":               69,
}

_PREPRINT_ONLY_FIELD_IDS = {61, 97, 69}  # repository, archiveID, genre


_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# ADS metadata helpers
# ---------------------------------------------------------------------------

def ads_full_metadata(bibcode):
    """
    Fetch full journal metadata from ADS for a confirmed journal bibcode.
    Returns a dict with: journal_doi, pub, volume, issue, pages, pubdate.
    """
    tok = ads_token()
    if not tok or not bibcode:
        return {}
    try:
        docs = ads_get("search/query", {
            "q": f"bibcode:{bibcode}",
            "rows": 1,
            "fl": "doi,pub,volume,issue,page,page_range,pubdate",
        }).get("response", {}).get("docs", [])
        if not docs:
            return {}
        d = docs[0]
        dois = d.get("doi") or []
        if isinstance(dois, str):
            dois = [dois]
        journal_doi = next((x for x in dois if not x.lower().startswith("10.48550")), None)
        # page_range preferred (e.g. "A41"), fall back to page list
        page = d.get("page_range")
        if not page:
            pg = d.get("page") or []
            page = pg[0] if pg else None
        return {
            "journal_doi": journal_doi,
            "pub":         d.get("pub"),
            "volume":      str(d.get("volume") or "").strip() or None,
            "issue":       str(d.get("issue")  or "").strip() or None,
            "pages":       str(page            or "").strip() or None,
            "pubdate":     d.get("pubdate"),
        }
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Unpaywall (publisher-only)
# ---------------------------------------------------------------------------

def _unpaywall_publisher_pdf(doi):
    """Return a publisher-hosted PDF URL from Unpaywall, or None."""
    try:
        url = f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi)}?email={EMAIL}"
        with urllib.request.urlopen(url, timeout=15) as r:
            d = _json.loads(r.read())
        locs = [l for l in (d.get("oa_locations") or [])
                if l.get("url_for_pdf") and l.get("host_type") == "publisher"]
        return locs[0]["url_for_pdf"] if locs else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# PDF download
# ---------------------------------------------------------------------------

def fetch_publisher_pdf(bibcode, journal_doi, dest):
    """
    Try to get the publisher version of record. Priority:
      1. ADS link_gateway (authenticated) — works for A&A/EDP Sciences
      2. ADS direct esource URLs
      3. Unpaywall publisher-only copy
    Deliberately excludes EPRINT_PDF and direct arXiv.
    Returns (success, source_label).
    """
    try:
        if _fp.ads_gateway_download(bibcode, dest):
            return True, "ADS-gateway"
    except Exception:
        try: os.remove(dest)
        except OSError: pass

    priority_urls, _ = _fp.ads_esource_urls(bibcode)
    for url in priority_urls:
        try:
            if download(url, dest) and is_pdf(dest):
                return True, "ADS"
        except Exception:
            try: os.remove(dest)
            except OSError: pass

    if journal_doi:
        u = _unpaywall_publisher_pdf(journal_doi)
        if u:
            try:
                if download(u, dest) and is_pdf(dest):
                    return True, "Unpaywall"
            except Exception:
                try: os.remove(dest)
                except OSError: pass

    # 4. Authenticated browser session (version of record behind the paywall)
    ok, src = _fp.browser_fetch_pdf(journal_doi, bibcode, dest, verbose=True)
    if ok:
        return True, src

    return False, None


# ---------------------------------------------------------------------------
# Zotero field helpers
# ---------------------------------------------------------------------------

def zotero_set_field(zc, item_id, field_id, value):
    """Insert or update a single field on a Zotero item. No-op if value is empty."""
    if value is None:
        return
    value = str(value).strip()
    if not value:
        return
    zc.execute("INSERT OR IGNORE INTO itemDataValues(value) VALUES(?)", (value,))
    vid = zc.execute(
        "SELECT valueID FROM itemDataValues WHERE value=?", (value,)).fetchone()["valueID"]
    existing = zc.execute(
        "SELECT valueID FROM itemData WHERE itemID=? AND fieldID=?",
        (item_id, field_id)).fetchone()
    if existing:
        if existing["valueID"] != vid:
            zc.execute("UPDATE itemData SET valueID=? WHERE itemID=? AND fieldID=?",
                       (vid, item_id, field_id))
    else:
        zc.execute("INSERT INTO itemData(itemID, fieldID, valueID) VALUES(?,?,?)",
                   (item_id, field_id, vid))


def delete_zotero_pdf_attachments(zc, parent_item_id):
    """
    Delete all PDF attachments from a Zotero item: marks deleted in DB and
    removes physical files. Returns count of attachments removed.
    """
    rows = zc.execute("""
        SELECT i.itemID, i.key, ia.path FROM itemAttachments ia
        JOIN items i ON i.itemID=ia.itemID
        WHERE ia.parentItemID=?
          AND ia.contentType='application/pdf'
          AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
    """, (parent_item_id,)).fetchall()

    for row in rows:
        zc.execute("INSERT OR IGNORE INTO deletedItems(itemID) VALUES(?)", (row["itemID"],))
        fname = row["path"].replace("storage:", "")
        fpath = os.path.join(STORAGE_DIR, row["key"], fname)
        try:
            os.remove(fpath)
        except OSError:
            pass
        try:
            os.rmdir(os.path.join(STORAGE_DIR, row["key"]))
        except OSError:
            pass

    return len(rows)


def promote_to_journal_article(zc, item_id, meta, bibcode=None):
    """
    Convert a Zotero item (typically preprint) to journalArticle type and
    populate journal metadata from the ADS metadata dict.
    """
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Change item type to journalArticle
    zc.execute("UPDATE items SET itemTypeID=?, dateModified=? WHERE itemID=?",
               (_TYPE_JOURNAL, now, item_id))

    # Remove preprint-only fields (repository, archiveID, genre)
    zc.execute("DELETE FROM itemData WHERE itemID=? AND fieldID IN (61,97,69)", (item_id,))

    # Update bibliographic fields
    if meta.get("journal_doi"):
        zotero_set_field(zc, item_id, _FLD["DOI"],  meta["journal_doi"])
        zotero_set_field(zc, item_id, _FLD["url"],  f"https://doi.org/{meta['journal_doi']}")
    if meta.get("pub"):
        zotero_set_field(zc, item_id, _FLD["publicationTitle"],    meta["pub"])
        zotero_set_field(zc, item_id, _FLD["libraryCatalog"],      meta["pub"])
    if meta.get("volume"):
        zotero_set_field(zc, item_id, _FLD["volume"], meta["volume"])
    if meta.get("issue"):
        zotero_set_field(zc, item_id, _FLD["issue"],  meta["issue"])
    if meta.get("pages"):
        zotero_set_field(zc, item_id, _FLD["pages"],  meta["pages"])
    if meta.get("pubdate"):
        # ADS format is "YYYY-MM-00"; strip trailing -00
        pd = re.sub(r"-00$", "", meta["pubdate"])
        zotero_set_field(zc, item_id, _FLD["date"], pd)

    # Clean extra field: remove arXiv header lines, keep/add ADS Bibcode
    extra_row = zc.execute("""
        SELECT dv.value FROM itemData id
        JOIN itemDataValues dv ON dv.valueID=id.valueID
        WHERE id.itemID=? AND id.fieldID=16
    """, (item_id,)).fetchone()
    old_extra = extra_row["value"] if extra_row else ""
    lines = [l for l in old_extra.split("\n")
             if not re.match(r"arXiv:\s*\S+", l.strip(), re.IGNORECASE)
             and not re.match(r"Type:\s*", l.strip(), re.IGNORECASE)]
    # Ensure ADS Bibcode line is present
    if bibcode and not any(re.search(r"bibcode:", l, re.IGNORECASE) for l in lines):
        lines.append(f"ADS Bibcode: {bibcode}")
    new_extra = "\n".join(lines).strip()
    if new_extra != old_extra:
        if new_extra:
            zotero_set_field(zc, item_id, _FLD["extra"], new_extra)
        else:
            # Nothing left — delete the field entirely
            zc.execute("DELETE FROM itemData WHERE itemID=? AND fieldID=16", (item_id,))


# ---------------------------------------------------------------------------
# Main upgrade loop
# ---------------------------------------------------------------------------

# arXiv-overlay journals: peer review only, no publisher-typeset PDF exists.
_OVERLAY_JOURNALS = {"OJAp"}


def _bibstem(bibcode):
    """Journal token of an ADS bibcode: '2025OJAp....850356K' -> 'OJAp'."""
    return (bibcode or "")[4:9].rstrip(".")


def upgrade(dry_run=False, limit=None, verbose=False, only=None):
    pa = sqlite3.connect(DB);        pa.row_factory = sqlite3.Row
    zc = sqlite3.connect(ZOTERO_DB); zc.row_factory = sqlite3.Row

    candidates = pa.execute("""
        SELECT id, citekey, title, authors, year, doi, arxiv, bibcode,
               journal, volume, issue, pages
        FROM papers
        WHERE (doi LIKE '10.48550/arxiv.%' OR doi LIKE '10.48550/arXiv.%')
          AND bibcode IS NOT NULL
          AND bibcode NOT LIKE '%arXiv%'
          AND pdf_path IS NOT NULL
        ORDER BY year DESC
    """).fetchall()

    # Overlay journals publish no PDF of their own — the arXiv copy *is* the
    # version of record, so there is nothing to upgrade to and these would
    # otherwise resurface as candidates on every run.
    skipped_overlay = [r for r in candidates if _bibstem(r["bibcode"]) in _OVERLAY_JOURNALS]
    candidates      = [r for r in candidates if _bibstem(r["bibcode"]) not in _OVERLAY_JOURNALS]
    if skipped_overlay and only is None:
        print(f"Skipping {len(skipped_overlay)} overlay-journal paper(s) "
              f"({', '.join(sorted(_OVERLAY_JOURNALS))}) — arXiv is the version of record")

    if only is not None:        # target one paper by id or citekey
        candidates = [r for r in candidates
                      if str(r["id"]) == str(only) or r["citekey"] == only]

    print(f"Found {len(candidates)} papers with arXiv DOIs confirmed published in journals")
    if not candidates:
        pa.close(); zc.close(); return

    stats = {"upgraded": 0, "unavailable": 0, "no_zotero": 0, "skipped": 0}
    touched_lists = set()

    for row in candidates:
        if limit and stats["upgraded"] >= limit:
            break

        pid     = row["id"]
        bibcode = row["bibcode"]
        arxiv   = _fp._extract_arxiv(row["arxiv"], row["doi"])
        title   = row["title"] or ""
        authors = row["authors"] or ""
        year    = row["year"]
        label   = f"[{row['citekey'] or pid}] {title[:55]}"

        if dry_run:
            print(f"  ~ {label}")
            print(f"    bibcode={bibcode}")
            stats["skipped"] += 1
            continue

        doi_arXiv = _fp._norm_doi(row["doi"])
        zp_id, _ = _fp.find_zotero_parent(zc, doi_arXiv, arxiv, row["citekey"])
        if not zp_id:
            if verbose:
                print(f"  ✗ {label}\n    not found in Zotero — skipping")
            stats["no_zotero"] += 1
            continue

        # Fetch full ADS metadata (journal DOI, pub, volume, issue, pages, date)
        meta = ads_full_metadata(bibcode)
        journal_doi = meta.get("journal_doi")

        if verbose:
            print(f"  ↓ {label}")
            print(f"    bibcode={bibcode}  journal_doi={journal_doi or '(none)'}")

        storage_key  = _fp.new_zotero_key(zc)
        fname        = _fp.zotero_filename(authors, year, title)
        storage_path = os.path.join(STORAGE_DIR, storage_key)
        os.makedirs(storage_path, exist_ok=True)
        dest         = os.path.join(storage_path, fname)

        ok, source = fetch_publisher_pdf(bibcode, journal_doi, dest)

        if ok:
            mtime = os.path.getmtime(dest)

            # Remove all old PDF attachments first
            n_removed = delete_zotero_pdf_attachments(zc, zp_id)

            # Convert item type and update metadata
            promote_to_journal_article(zc, zp_id, meta, bibcode=bibcode)

            # Register new PDF attachment
            _fp.insert_zotero_attachment(zc, zp_id, storage_key, fname, mtime)

            # Update papers-vela DB
            pa.execute("""
                UPDATE papers SET
                  pdf_path=?,
                  doi=?,
                  journal=?,
                  volume=?,
                  issue=?,
                  pages=?
                WHERE id=?
            """, (
                dest,
                journal_doi or row["doi"],
                meta.get("pub") or row["journal"] or None,
                meta.get("volume") or row["volume"] or None,
                meta.get("issue")  or row["issue"]  or None,
                meta.get("pages")  or row["pages"]  or None,
                pid,
            ))

            for r in pa.execute(
                "SELECT l.name FROM lists l JOIN paper_lists pl "
                "ON pl.list_id=l.id WHERE pl.paper_id=?", (pid,)):
                touched_lists.add(r["name"])

            stats["upgraded"] += 1
            if verbose:
                print(f"    ✓ [{source}] removed {n_removed} old PDF(s)"
                      f" → Zotero storage/{storage_key}/")
        else:
            try: os.rmdir(storage_path)
            except OSError: pass
            stats["unavailable"] += 1
            if verbose:
                print(f"    ✗ no open-access publisher PDF found")

    zc.commit()
    pa.commit()

    print(f"\nDone: {stats['upgraded']} upgraded"
          f"  |  {stats['unavailable']} no publisher PDF available"
          f"  |  {stats['no_zotero']} not found in Zotero"
          f"  |  {stats['skipped']} dry-run skipped")

    if stats["upgraded"] and not dry_run:
        n = reindex_lists(pa, list(touched_lists) if touched_lists else None)
        pa.commit()
        print(f"Reindexed {n} lists")
        import subprocess as sp
        gen = os.path.join(PAPERS_VELA, "generate_outputs.py")
        r = sp.run([sys.executable, gen],
                   env={**os.environ, "PAPERS_LIB": LIB},
                   capture_output=True, text=True)
        print("references.bib + INDEX.md regenerated" if r.returncode == 0
              else f"bib warning: {r.stderr.strip()}")

    pa.close()
    zc.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run",  action="store_true",
                    help="show candidates without downloading")
    ap.add_argument("--limit",    type=int, default=None,
                    help="stop after N successful upgrades")
    ap.add_argument("--verbose",  action="store_true")
    ap.add_argument("--no-check", action="store_true",
                    help="skip Zotero-running check (dangerous)")
    ap.add_argument("--id", dest="only", default=None,
                    help="upgrade only this paper (papers-vela id or citekey)")
    a = ap.parse_args()

    for path, label in [(DB, "papers-vela library"), (ZOTERO_DB, "Zotero database")]:
        if not os.path.exists(path):
            sys.exit(f"{label} not found at {path}")

    if not a.dry_run and not a.no_check:
        if _fp.zotero_is_running():
            sys.exit(
                "⚠  Zotero is currently running.\n"
                "   Close Zotero first, then re-run.\n"
                "   (Use --no-check to override — at your own risk.)")

    upgrade(dry_run=a.dry_run, limit=a.limit, verbose=a.verbose, only=a.only)


if __name__ == "__main__":
    main()
