#!/usr/bin/env python3
"""
attach_from_downloads.py — match PDFs dropped into ~/Downloads to arXiv-upgrade
candidates and perform the full upgrade (same pipeline as upgrade_arxiv_pdfs.py
but using the already-downloaded local file instead of fetching).

Workflow:
  1. Fetch journal metadata (DOI, pub, volume…) from ADS for all 37 candidates.
  2. Open all publisher pages in Firefox so the user can download the PDFs.
  3. User drops PDFs into ~/Downloads.
  4. Close Zotero, then run this script (--attach).
  5. Each PDF is scanned for an embedded DOI, matched to a candidate, and upgraded.

⚠  REQUIRES ZOTERO TO BE CLOSED — writes directly to zotero.sqlite.

Usage:
  # Step 2 — open all publisher pages in Firefox tabs:
  python3 attach_from_downloads.py --open-tabs

  # Step 4 — attach PDFs already in ~/Downloads:
  python3 attach_from_downloads.py --attach [--downloads DIR] [--dry-run] [--verbose]
"""

import argparse, os, re, shutil, sqlite3, subprocess, sys, time

PAPERS_VELA = os.path.expanduser(os.environ.get("PAPERS_VELA") or os.environ.get("PAPERS_AGENT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PAPERS_VELA)

import importlib.util as _ilu

def _load(name, path):
    spec = _ilu.spec_from_file_location(name, path)
    mod  = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

_up = _load("upgrade_arxiv_pdfs", os.path.join(PAPERS_VELA, "zotero", "upgrade_arxiv_pdfs.py"))
_fp = _up._fp

from papers import ads_token, ads_get, reindex_lists
import urllib.parse

LIB         = os.path.expanduser(os.environ.get("PAPERS_LIB") or next((p for p in ("~/Zotero/papers-vela", "~/Zotero/papers-agent")  # the pre-rename name still works
                if os.path.isdir(os.path.expanduser(p))), "~/Zotero/papers-vela"))
DB          = os.path.join(LIB, "library.db")
ZOTERO_DB   = os.path.expanduser(os.environ.get("ZOTERO_DB",  "~/Zotero/zotero.sqlite"))
ZOTERO_DIR  = os.path.expanduser(os.environ.get("ZOTERO_DIR", "~/Zotero"))
STORAGE_DIR = os.path.join(ZOTERO_DIR, "storage")
DOWNLOADS   = os.path.expanduser("~/Downloads")


# ---------------------------------------------------------------------------
# DOI extraction from raw PDF bytes
# ---------------------------------------------------------------------------

_DOI_RE = re.compile(rb'10\.\d{4,}/[^\s<>"\'\\,;)({}[\]]+')
_TRAILING = b'.,;)}\\]'


def extract_dois(data):
    """Return a set of normalised DOIs found in raw PDF bytes."""
    found = set()
    for m in _DOI_RE.finditer(data):
        raw = m.group(0).rstrip(_TRAILING)
        try:
            doi = raw.decode("ascii", errors="ignore").lower().strip()
        except Exception:
            continue
        if doi and not doi.startswith("10.48550"):   # skip arXiv DOIs
            found.add(doi)
    return found


# ---------------------------------------------------------------------------
# Batch ADS lookup — one query for all candidates
# ---------------------------------------------------------------------------

def fetch_all_metadata(candidates):
    """Return {citekey: meta_dict} via a single batched ADS query."""
    tok = ads_token()
    if not tok:
        return {}
    bibcodes = [r["bibcode"] for r in candidates if r["bibcode"]]
    if not bibcodes:
        return {}
    # ADS allows bibcode:(X OR Y OR …) — batch up to 100
    bq = " OR ".join(f'"{b}"' for b in bibcodes)
    try:
        docs = ads_get("search/query", {
            "q":    f"bibcode:({bq})",
            "rows": len(bibcodes) + 5,
            "fl":   "bibcode,doi,pub,volume,issue,page,page_range,pubdate",
        }).get("response", {}).get("docs", [])
    except Exception as e:
        print(f"[ads] batch query failed: {e}")
        return {}

    by_bibcode = {}
    for d in docs:
        bc = d.get("bibcode", "")
        dois = d.get("doi") or []
        if isinstance(dois, str):
            dois = [dois]
        journal_doi = next((x for x in dois if not x.lower().startswith("10.48550")), None)
        page = d.get("page_range")
        if not page:
            pg = d.get("page") or []
            page = pg[0] if pg else None
        by_bibcode[bc] = {
            "journal_doi": journal_doi,
            "pub":         d.get("pub"),
            "volume":      str(d.get("volume") or "").strip() or None,
            "issue":       str(d.get("issue")  or "").strip() or None,
            "pages":       str(page            or "").strip() or None,
            "pubdate":     d.get("pubdate"),
        }
    return by_bibcode


# ---------------------------------------------------------------------------
# Load candidates
# ---------------------------------------------------------------------------

def load_candidates():
    pa = sqlite3.connect(DB)
    pa.row_factory = sqlite3.Row
    rows = pa.execute("""
        SELECT id, citekey, title, authors, year, doi, arxiv, bibcode,
               journal, volume, issue, pages
        FROM papers
        WHERE (doi LIKE '10.48550/arxiv.%' OR doi LIKE '10.48550/arXiv.%')
          AND bibcode IS NOT NULL
          AND bibcode NOT LIKE '%arXiv%'
          AND pdf_path IS NOT NULL
        ORDER BY year DESC
    """).fetchall()
    pa.close()
    return rows


# ---------------------------------------------------------------------------
# --open-tabs
# ---------------------------------------------------------------------------

def open_tabs(candidates, meta_by_bibcode):
    FIREFOX = os.environ.get(
        "PAPERS_BROWSER_BIN",
        "/Applications/Firefox.app/Contents/MacOS/firefox"
    )
    opened = 0
    for row in candidates:
        meta = meta_by_bibcode.get(row["bibcode"] or "", {})
        doi  = meta.get("journal_doi")
        url  = f"https://doi.org/{urllib.parse.quote(doi, safe='/:')}" if doi else None
        if not url:
            print(f"  ✗ [{row['citekey']}] no journal DOI — skipping tab")
            continue
        label = (row["title"] or "")[:60]
        print(f"  → [{row['citekey']}] {label}")
        subprocess.Popen([FIREFOX, "--new-tab", url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.8)   # let Firefox register each tab before the next
        opened += 1
    print(f"\nOpened {opened} tab(s). Download the PDFs to ~/Downloads, "
          f"then close Zotero and run:\n"
          f"  python3 attach_from_downloads.py --attach --verbose")


# ---------------------------------------------------------------------------
# --attach
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--open-tabs", action="store_true",
                    help="open all journal DOI pages in Firefox")
    ap.add_argument("--attach",    action="store_true",
                    help="scan ~/Downloads and attach matched PDFs")
    ap.add_argument("--downloads", default=DOWNLOADS,
                    help=f"directory to scan (default: {DOWNLOADS})")
    ap.add_argument("--dry-run",   action="store_true")
    ap.add_argument("--verbose",   action="store_true")
    a = ap.parse_args()

    if not a.open_tabs and not a.attach:
        ap.print_help(); sys.exit(0)

    candidates = load_candidates()
    print(f"Found {len(candidates)} arXiv-upgrade candidates")

    if a.open_tabs or a.attach:
        print("Fetching journal metadata from ADS (one batch)…")
        meta_by_bibcode = fetch_all_metadata(candidates)

    if a.open_tabs:
        open_tabs(candidates, meta_by_bibcode)

    if a.attach:
        _attach(candidates, meta_by_bibcode, a.downloads, a.dry_run, a.verbose)


def _attach(candidates, meta_by_bibcode, downloads_dir, dry_run, verbose):
    """Inner attach logic; reuses already-fetched ADS metadata."""
    doi_map = {}
    no_doi  = []
    for row in candidates:
        meta = meta_by_bibcode.get(row["bibcode"] or "", {})
        jdoi = (meta.get("journal_doi") or "").lower().strip()
        if jdoi:
            doi_map[jdoi] = (row, meta)
        else:
            no_doi.append(row["citekey"])

    if no_doi:
        print(f"  [warn] no journal DOI from ADS for: {', '.join(no_doi)}")

    pdf_files = sorted(f for f in os.listdir(downloads_dir) if f.lower().endswith(".pdf"))
    print(f"[attach] {len(pdf_files)} PDF(s) in {downloads_dir}")
    if not pdf_files:
        print("Nothing to do."); return

    pa = sqlite3.connect(DB);        pa.row_factory = sqlite3.Row
    zc = sqlite3.connect(ZOTERO_DB); zc.row_factory = sqlite3.Row

    stats  = {"upgraded": 0, "no_match": 0, "skipped": 0}
    touched_lists = set()

    for fname in pdf_files:
        path = os.path.join(downloads_dir, fname)
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            continue

        if not data.startswith(b"%PDF"):
            continue

        dois = extract_dois(data)
        matched_doi = next((d for d in dois if d in doi_map), None)

        if not matched_doi:
            if verbose:
                print(f"  ? {fname} — no matching DOI (found: {sorted(dois)[:3]})")
            stats["no_match"] += 1
            continue

        row, meta = doi_map[matched_doi]
        label = f"[{row['citekey']}] {(row['title'] or '')[:55]}"

        if dry_run:
            print(f"  ~ {fname} → {label}")
            stats["skipped"] += 1
            continue

        pid       = row["id"]
        arxiv     = _fp._extract_arxiv(row["arxiv"], row["doi"])
        doi_arXiv = _fp._norm_doi(row["doi"])
        zp_id, _  = _fp.find_zotero_parent(zc, doi_arXiv, arxiv, row["citekey"])

        if not zp_id:
            print(f"  ✗ {label} — not found in Zotero")
            stats["no_match"] += 1
            continue

        storage_key  = _fp.new_zotero_key(zc)
        new_fname    = _fp.zotero_filename(row["authors"], row["year"], row["title"])
        storage_path = os.path.join(STORAGE_DIR, storage_key)
        os.makedirs(storage_path, exist_ok=True)
        dest         = os.path.join(storage_path, new_fname)

        shutil.copy2(path, dest)
        mtime = os.path.getmtime(dest)

        _up.delete_zotero_pdf_attachments(zc, zp_id)
        _up.promote_to_journal_article(zc, zp_id, meta, bibcode=row["bibcode"])
        _fp.insert_zotero_attachment(zc, zp_id, storage_key, new_fname, mtime)

        journal_doi = meta.get("journal_doi") or row["doi"]
        pa.execute("""
            UPDATE papers SET
              pdf_path=?, doi=?, journal=?, volume=?, issue=?, pages=?
            WHERE id=?
        """, (
            dest,
            journal_doi,
            meta.get("pub")    or row["journal"] or None,
            meta.get("volume") or row["volume"]  or None,
            meta.get("issue")  or row["issue"]   or None,
            meta.get("pages")  or row["pages"]   or None,
            pid,
        ))

        for r in pa.execute(
            "SELECT l.name FROM lists l JOIN paper_lists pl "
            "ON pl.list_id=l.id WHERE pl.paper_id=?", (pid,)):
            touched_lists.add(r["name"])

        stats["upgraded"] += 1
        print(f"  ✓ {label}")
        if verbose:
            print(f"    {fname} → storage/{storage_key}/")

    zc.commit(); pa.commit()

    print(f"\nDone: {stats['upgraded']} upgraded  |  "
          f"{stats['no_match']} unmatched  |  {stats['skipped']} dry-run skipped")

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

    pa.close(); zc.close()


if __name__ == "__main__":
    main()
