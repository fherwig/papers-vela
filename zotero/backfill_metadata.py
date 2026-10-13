#!/usr/bin/env python3
"""
backfill_metadata.py — fill in missing journal/volume/issue/pages from ADS.

Targets papers-vela records that have a journal DOI (not 10.48550/arxiv.*)
but are missing one or more of: journal, volume, issue, pages.

For each paper it:
  1. Looks up the ADS record (by bibcode if available, else by DOI)
  2. Updates papers-vela: journal, volume, issue, pages
  3. Optionally updates the matching Zotero item (--update-zotero, Zotero must be closed)

Safe to run while Zotero is open UNLESS --update-zotero is passed.

Usage:
  python3 backfill_metadata.py [--dry-run] [--verbose] [--update-zotero] [--limit N]

Env vars: PAPERS_LIB, PAPERS_VELA, ZOTERO_DB, ZOTERO_DIR (same as upgrade_arxiv_pdfs.py)
"""

import argparse, os, sys, sqlite3, datetime

PAPERS_VELA = os.path.expanduser(os.environ.get("PAPERS_VELA") or os.environ.get("PAPERS_AGENT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PAPERS_VELA)

import importlib.util as _ilu

def _load(name, path):
    spec = _ilu.spec_from_file_location(name, path)
    mod  = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

_up = _load("upgrade", os.path.join(PAPERS_VELA, "zotero", "upgrade_arxiv_pdfs.py"))

from papers import ads_get, ads_token

LIB       = os.path.expanduser(os.environ.get("PAPERS_LIB") or next((p for p in ("~/Zotero/papers-vela", "~/Zotero/papers-agent")  # the pre-rename name still works
                if os.path.isdir(os.path.expanduser(p))), "~/Zotero/papers-vela"))
DB        = os.path.join(LIB, "library.db")
ZOTERO_DB = os.path.expanduser(os.environ.get("ZOTERO_DB",  "~/Zotero/zotero.sqlite"))


# ---------------------------------------------------------------------------
# ADS lookup
# ---------------------------------------------------------------------------

def ads_meta_by_bibcode(bibcode):
    """Fetch pub/volume/issue/pages/pubdate from ADS by bibcode."""
    return _up.ads_full_metadata(bibcode)


def ads_meta_by_doi(doi):
    """Fetch pub/volume/issue/pages/pubdate from ADS by DOI (for records with no bibcode)."""
    try:
        docs = ads_get("search/query", {
            "q": f"doi:{doi}",
            "rows": 1,
            "fl": "bibcode,doi,pub,volume,issue,page,page_range,pubdate",
        }).get("response", {}).get("docs", [])
        if not docs:
            return {}, None
        d = docs[0]
        page = d.get("page_range") or (d.get("page") or [None])[0]
        meta = {
            "pub":     d.get("pub"),
            "volume":  str(d.get("volume") or "").strip() or None,
            "issue":   str(d.get("issue")  or "").strip() or None,
            "pages":   str(page            or "").strip() or None,
            "pubdate": d.get("pubdate"),
        }
        return meta, d.get("bibcode")
    except Exception:
        return {}, None


# ---------------------------------------------------------------------------
# Zotero helpers (reused from upgrade_arxiv_pdfs)
# ---------------------------------------------------------------------------

def open_zotero_rw():
    """Open Zotero in read-write mode (requires Zotero to be closed)."""
    zc = sqlite3.connect(ZOTERO_DB)
    zc.row_factory = sqlite3.Row
    return zc


def zotero_item_for_doi(zc, doi):
    """Find the Zotero itemID for a given journal DOI (case-insensitive)."""
    rows = zc.execute("""
        SELECT i.itemID FROM items i
        JOIN itemData id2 ON id2.itemID=i.itemID AND id2.fieldID=59
        JOIN itemDataValues idv ON idv.valueID=id2.valueID
        WHERE LOWER(idv.value)=LOWER(?) AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
    """, (doi,)).fetchall()
    return rows[0]["itemID"] if rows else None


def zotero_item_for_bibcode(zc, bibcode):
    """Find Zotero itemID by searching extra field for 'ADS Bibcode: <bc>'."""
    rows = zc.execute("""
        SELECT i.itemID FROM items i
        JOIN itemData id2 ON id2.itemID=i.itemID AND id2.fieldID=16
        JOIN itemDataValues idv ON idv.valueID=id2.valueID
        WHERE idv.value LIKE ? AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
    """, (f"%{bibcode}%",)).fetchall()
    return rows[0]["itemID"] if rows else None


def update_zotero_meta(zc, item_id, meta, verbose=False):
    """Write pub/volume/issue/pages/date into a Zotero item's fields."""
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    updates = []
    if meta.get("pub"):
        _up.zotero_set_field(zc, item_id, _up._FLD["publicationTitle"], meta["pub"])
        updates.append(f"journal={meta['pub']}")
    if meta.get("volume"):
        _up.zotero_set_field(zc, item_id, _up._FLD["volume"], meta["volume"])
        updates.append(f"volume={meta['volume']}")
    if meta.get("issue"):
        _up.zotero_set_field(zc, item_id, _up._FLD["issue"], meta["issue"])
        updates.append(f"issue={meta['issue']}")
    if meta.get("pages"):
        _up.zotero_set_field(zc, item_id, _up._FLD["pages"], meta["pages"])
        updates.append(f"pages={meta['pages']}")
    if meta.get("pubdate"):
        _up.zotero_set_field(zc, item_id, _up._FLD["date"], meta["pubdate"][:7])
        updates.append(f"date={meta['pubdate'][:7]}")
    if updates:
        zc.execute("UPDATE items SET dateModified=? WHERE itemID=?", (now, item_id))
    return updates


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(dry_run=False, verbose=False, update_zotero=False, limit=None):
    pa = sqlite3.connect(DB)
    pa.row_factory = sqlite3.Row

    rows = pa.execute("""
        SELECT id, citekey, doi, bibcode, journal, volume, issue, pages, title
        FROM papers
        WHERE doi NOT LIKE '10.48550/%%'
          AND doi NOT LIKE 'http%%'
          AND doi IS NOT NULL AND doi != ''
          AND (
            journal IS NULL OR journal = ''
            OR volume  IS NULL OR volume  = ''
            OR pages   IS NULL OR pages   = ''
          )
        ORDER BY year DESC
    """).fetchall()

    if limit:
        rows = rows[:limit]

    print(f"[backfill] {len(rows)} papers with incomplete metadata\n")

    zc = None
    if update_zotero and not dry_run:
        zc = open_zotero_rw()

    ok = skip = err = 0

    for row in rows:
        pid      = row["id"]
        citekey  = row["citekey"]
        doi      = row["doi"]
        bibcode  = row["bibcode"]
        missing  = [f for f, v in [("journal", row["journal"]),
                                   ("volume",  row["volume"]),
                                   ("pages",   row["pages"])]
                    if not v]

        if verbose:
            print(f"  [{citekey}]  missing: {', '.join(missing)}")

        # Fetch from ADS
        new_bibcode = None
        if bibcode:
            meta = ads_meta_by_bibcode(bibcode)
        else:
            meta, new_bibcode = ads_meta_by_doi(doi)

        if not any(meta.get(k) for k in ("pub", "volume", "pages")):
            print(f"  SKIP [{citekey}] — ADS returned no metadata")
            skip += 1
            continue

        if (meta.get("pub") or "").lower().startswith("arxiv"):
            print(f"  SKIP [{citekey}] — ADS pub is arXiv (not yet published in journal)")
            skip += 1
            continue

        if verbose:
            print(f"         → pub={meta.get('pub')} vol={meta.get('volume')} "
                  f"iss={meta.get('issue')} pages={meta.get('pages')}")

        if dry_run:
            ok += 1
            continue

        # Update papers-vela
        pa.execute("""
            UPDATE papers SET
              journal = COALESCE(NULLIF(journal,''), ?),
              volume  = COALESCE(NULLIF(volume, ''), ?),
              issue   = COALESCE(NULLIF(issue,  ''), ?),
              pages   = COALESCE(NULLIF(pages,  ''), ?)
              """ + (", bibcode = ?" if new_bibcode else "") + """
            WHERE id = ?
        """, [
            meta.get("pub"),
            meta.get("volume"),
            meta.get("issue"),
            meta.get("pages"),
            *(([new_bibcode]) if new_bibcode else []),
            pid,
        ])

        # Update Zotero if requested
        if zc:
            item_id = zotero_item_for_doi(zc, doi)
            if not item_id and bibcode:
                item_id = zotero_item_for_bibcode(zc, bibcode)
            if item_id:
                updates = update_zotero_meta(zc, item_id, meta, verbose=verbose)
                if verbose and updates:
                    print(f"         Zotero updated: {', '.join(updates)}")
            elif verbose:
                print(f"         Zotero item not found for doi={doi}")

        ok += 1

    if not dry_run:
        pa.commit()
        if zc:
            zc.commit()
            zc.close()

    pa.close()
    print(f"\n[backfill] done — {ok} updated, {skip} skipped (no ADS data), {err} errors")
    if dry_run:
        print("(dry run — no changes written)")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run",       action="store_true")
    ap.add_argument("--verbose",  "-v", action="store_true")
    ap.add_argument("--update-zotero", action="store_true",
                    help="also update Zotero item fields (requires Zotero closed)")
    ap.add_argument("--limit", type=int, help="process at most N papers")
    a = ap.parse_args()

    if not ads_token():
        sys.exit("ADS token not found at ~/.config/ads/token")
    if not os.path.exists(DB):
        sys.exit(f"papers-vela DB not found at {DB}")

    run(dry_run=a.dry_run, verbose=a.verbose,
        update_zotero=a.update_zotero, limit=a.limit)


if __name__ == "__main__":
    main()
