#!/usr/bin/env python3
"""
zotero_fetch_pdfs.py — download missing PDFs into Zotero storage so both
Zotero and papers-vela point at the identical file.

Download priority:
  1. ADS resolver — PUB_PDF, AUTHOR_PDF, ADS_PDF, ADS_SCAN (publisher/hosted copies)
  2. Unpaywall    — open-access publisher copy
  3. ADS resolver — EPRINT_PDF (arXiv via ADS)
  4. Direct arXiv — fallback

Before downloading, if the Zotero item has no ADS bibcode in its 'extra' field,
the bibcode is looked up via ADS and written back to Zotero. If the item already
has a stored PDF attachment in Zotero, papers-vela's pdf_path is updated to that
file instead of downloading a second copy.

⚠  REQUIRES ZOTERO TO BE CLOSED — this writes to zotero.sqlite directly.
   Zotero will discover the new attachments on next launch.

Usage:
  python3 zotero_fetch_pdfs.py [options]

Options:
  --dry-run     show what would be fetched, don't download or write
  --limit N     stop after N successful downloads (default: unlimited)
  --verbose     print each attempt
  --no-check    skip the Zotero-running check (use with care)

Env vars (user-configured — no built-in defaults):
  PAPERS_LIB    papers-vela library dir
  PAPERS_VELA  papers-vela repo dir
  ZOTERO_DB     zotero.sqlite path         (default ~/Zotero/zotero.sqlite)
  ZOTERO_DIR    Zotero base dir            (default ~/Zotero)
"""

import argparse, datetime, os, random, re, sqlite3, string, subprocess, sys, unicodedata

# PAPERS_VELA (the repo clone) defaults to this script's own repo; PAPERS_LIB is required (no default).
PAPERS_VELA = os.environ.get("PAPERS_VELA") or os.environ.get("PAPERS_AGENT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.expanduser(PAPERS_VELA))
from papers import (last_name, keywords as make_keywords, reindex_lists,
                    ads_token, ads_get, download, is_pdf, unpaywall_pdf, require_lib)

LIB         = os.path.expanduser(os.environ.get("PAPERS_LIB") or "")   # REQUIRED, no default
DB          = os.path.join(LIB, "library.db")
PDFS_DIR    = os.path.join(LIB, "pdfs")
ZOTERO_DB   = os.path.expanduser(os.environ.get("ZOTERO_DB",  "~/Zotero/zotero.sqlite"))
ZOTERO_DIR  = os.path.expanduser(os.environ.get("ZOTERO_DIR", "~/Zotero"))
STORAGE_DIR = os.path.join(ZOTERO_DIR, "storage")
LIBRARY_ID  = 1


# ---------------------------------------------------------------------------
# Zotero safety check
# ---------------------------------------------------------------------------

def zotero_is_running():
    try:
        r = subprocess.run(["pgrep", "-i", "zotero"], capture_output=True)
        return r.returncode == 0
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------
# Zotero key + filename helpers
# ---------------------------------------------------------------------------

_KEY_CHARS = string.ascii_uppercase + string.digits


def new_zotero_key(zc):
    existing = {r[0] for r in zc.execute("SELECT key FROM items")}
    for _ in range(1000):
        k = "".join(random.choices(_KEY_CHARS, k=8))
        if k not in existing:
            return k
    raise RuntimeError("Could not generate a unique Zotero key")


def zotero_filename(authors, year, title, max_title=60):
    first = (authors or "").split(" and ")[0].strip()
    last  = first.split(",")[0].strip() if "," in first else (first.split() or ["Unknown"])[-1]
    last  = "".join(c for c in unicodedata.normalize("NFKD", last)
                    if not unicodedata.combining(c))
    n_au  = len([a for a in (authors or "").split(" and ") if a.strip()])
    author_part = f"{last} et al." if n_au > 1 else last
    safe_title  = re.sub(r'[\\/:*?"<>|]', "", title or "Untitled")
    safe_title  = re.sub(r"\s+", " ", safe_title).strip()[:max_title].rstrip()
    return f"{author_part} - {year or 'nd'} - {safe_title}.pdf"


# ---------------------------------------------------------------------------
# Identifier helpers
# ---------------------------------------------------------------------------

_ARXIV_DOI_RE = re.compile(r"10\.48550/arxiv\.([^\s/?#]+)", re.IGNORECASE)
_ARXIV_URL_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/([^\s/?#]+)", re.IGNORECASE)


def _norm_doi(s):
    """Return a real DOI (starts with 10.) or None — rejects bare URLs."""
    s = re.sub(r"^https?://(dx\.)?doi\.org/", "", (s or "").strip())
    return s if re.match(r"10\.\d{4}", s) else None


def _extract_arxiv(arxiv_col, doi_col):
    """Best-effort arXiv ID from the arxiv column or embedded in a DOI/URL field."""
    if arxiv_col:
        return re.sub(r"v\d+$", "", arxiv_col.strip())
    for src in (doi_col or "",):
        m = _ARXIV_DOI_RE.search(src) or _ARXIV_URL_RE.search(src)
        if m:
            return re.sub(r"v\d+$", "", m.group(1))
    return None


# ---------------------------------------------------------------------------
# ADS helpers
# ---------------------------------------------------------------------------

_ADS_PDF_PRIORITY = ["PUB_PDF", "AUTHOR_PDF", "ADS_PDF", "ADS_SCAN"]


def ads_lookup_bibcode(doi=None, arxiv=None):
    """Return ADS bibcode for a paper, or None if ADS is unavailable or no match."""
    tok = ads_token()
    if not tok:
        return None
    try:
        if doi:
            docs = ads_get("search/query",
                           {"q": f"doi:{doi}", "rows": 1, "fl": "bibcode"}
                           ).get("response", {}).get("docs", [])
            if docs:
                return docs[0].get("bibcode")
        if arxiv:
            docs = ads_get("search/query",
                           {"q": f"arxiv:{arxiv}", "rows": 1, "fl": "bibcode"}
                           ).get("response", {}).get("docs", [])
            if docs:
                return docs[0].get("bibcode")
    except Exception:
        pass
    return None


def ads_esource_urls(bibcode):
    """
    Return (priority_urls, eprint_url) from the ADS resolver.
    priority_urls: list of URLs for PUB_PDF/AUTHOR_PDF/ADS_PDF/ADS_SCAN (in that order).
    eprint_url: the EPRINT_PDF URL if present, else None.
    """
    tok = ads_token()
    if not tok or not bibcode:
        return [], None
    try:
        data = ads_get(f"resolver/{bibcode}/esource", {})
        records = data.get("links", {}).get("records", [])
        # ADS returns link_type as "ESOURCE|PUB_PDF" — normalise by stripping the prefix
        def _norm(lt): return lt.split("|")[-1] if "|" in lt else lt
        by_type = {_norm(r["link_type"]): r["url"]
                   for r in records if "link_type" in r and "url" in r}
        priority = [by_type[t] for t in _ADS_PDF_PRIORITY if t in by_type]
        eprint   = by_type.get("EPRINT_PDF")
        return priority, eprint
    except Exception:
        return [], None


# ---------------------------------------------------------------------------
# Zotero DB helpers
# ---------------------------------------------------------------------------

def find_zotero_parent(zc, doi, arxiv, citekey):
    """Return (itemID, key) of the Zotero parent item, or (None, None)."""
    if doi:
        r = zc.execute("""
            SELECT i.itemID, i.key FROM items i
            JOIN itemData id ON id.itemID=i.itemID
            JOIN itemDataValues dv ON dv.valueID=id.valueID
            JOIN fields f ON f.fieldID=id.fieldID AND f.fieldName='DOI'
            WHERE lower(dv.value)=?
              AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
        """, (doi.lower(),)).fetchone()
        if r: return r["itemID"], r["key"]

    if arxiv:
        r = zc.execute("""
            SELECT i.itemID, i.key FROM items i
            JOIN itemData id ON id.itemID=i.itemID
            JOIN itemDataValues dv ON dv.valueID=id.valueID
            JOIN fields f ON f.fieldID=id.fieldID AND f.fieldName='DOI'
            WHERE lower(dv.value) LIKE ?
              AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
        """, (f"%arxiv.{arxiv.lower()}%",)).fetchone()
        if r: return r["itemID"], r["key"]

        r = zc.execute("""
            SELECT i.itemID, i.key FROM items i
            JOIN itemData id ON id.itemID=i.itemID
            JOIN itemDataValues dv ON dv.valueID=id.valueID
            JOIN fields f ON f.fieldID=id.fieldID AND f.fieldName='url'
            WHERE dv.value LIKE ?
              AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
        """, (f"%arxiv.org%{arxiv}%",)).fetchone()
        if r: return r["itemID"], r["key"]

    if citekey:
        r = zc.execute("""
            SELECT i.itemID, i.key FROM items i
            JOIN itemData id ON id.itemID=i.itemID
            JOIN itemDataValues dv ON dv.valueID=id.valueID
            JOIN fields f ON f.fieldID=id.fieldID AND f.fieldName='citationKey'
            WHERE lower(dv.value)=?
              AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
        """, (citekey.lower(),)).fetchone()
        if r: return r["itemID"], r["key"]

    return None, None


def existing_zotero_pdf(zc, parent_item_id):
    """Return the absolute path of an existing stored PDF attachment, or None."""
    row = zc.execute("""
        SELECT i.key, ia.path FROM itemAttachments ia
        JOIN items i ON i.itemID = ia.itemID
        WHERE ia.parentItemID=?
          AND ia.linkMode=1
          AND ia.contentType='application/pdf'
          AND ia.itemID NOT IN (SELECT itemID FROM deletedItems)
        LIMIT 1
    """, (parent_item_id,)).fetchone()
    if not row:
        return None
    filename = row["path"].replace("storage:", "")
    path = os.path.join(STORAGE_DIR, row["key"], filename)
    return path if os.path.isfile(path) else None


# Matches both "ADS Bibcode:" (Zotero connector format) and bare "bibcode:"
_BC_RE = re.compile(r"(?im)^(?:ads\s+)?bibcode:\s*(\S+)")


def zotero_bibcode_in_extra(zc, item_id):
    """Return the bibcode stored in the item's extra field, or None."""
    row = zc.execute("""
        SELECT dv.value FROM itemData id
        JOIN itemDataValues dv ON dv.valueID=id.valueID
        JOIN fields f ON f.fieldID=id.fieldID AND f.fieldName='extra'
        WHERE id.itemID=?
    """, (item_id,)).fetchone()
    if not row:
        return None
    m = _BC_RE.search(row["value"])
    return m.group(1) if m else None


def write_zotero_bibcode(zc, item_id, bibcode):
    """Append 'ADS Bibcode: X' to the Zotero item's extra field (creates it if absent)."""
    field_row = zc.execute(
        "SELECT fieldID FROM fields WHERE fieldName='extra'").fetchone()
    if not field_row:
        return
    field_id = field_row["fieldID"]

    existing = zc.execute("""
        SELECT id.valueID, dv.value FROM itemData id
        JOIN itemDataValues dv ON dv.valueID=id.valueID
        WHERE id.itemID=? AND id.fieldID=?
    """, (item_id, field_id)).fetchone()

    bibcode_line = f"ADS Bibcode: {bibcode}"
    if existing:
        if _BC_RE.search(existing["value"]):
            return
        new_value = existing["value"].rstrip() + f"\n{bibcode_line}"
    else:
        new_value = bibcode_line

    # itemDataValues rows may be shared; always insert a new one
    zc.execute("INSERT OR IGNORE INTO itemDataValues(value) VALUES(?)", (new_value,))
    value_id = zc.execute(
        "SELECT valueID FROM itemDataValues WHERE value=?", (new_value,)).fetchone()["valueID"]

    if existing:
        zc.execute("UPDATE itemData SET valueID=? WHERE itemID=? AND fieldID=?",
                   (value_id, item_id, field_id))
    else:
        zc.execute("INSERT INTO itemData(itemID, fieldID, valueID) VALUES(?,?,?)",
                   (item_id, field_id, value_id))


def insert_zotero_attachment(zc, parent_item_id, storage_key, filename, file_mtime):
    now      = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mtime_ms = int(file_mtime * 1000)
    cur = zc.execute("""
        INSERT INTO items(itemTypeID, dateAdded, dateModified, clientDateModified,
                          libraryID, key, version, synced)
        VALUES(3, ?, ?, ?, ?, ?, 0, 0)
    """, (now, now, now, LIBRARY_ID, storage_key))
    attach_id = cur.lastrowid
    zc.execute("""
        INSERT INTO itemAttachments(itemID, parentItemID, linkMode, contentType,
                                    charsetID, path, syncState, storageModTime,
                                    storageHash, lastProcessedModificationTime)
        VALUES(?, ?, 1, 'application/pdf', NULL, ?, 0, ?, NULL, ?)
    """, (attach_id, parent_item_id, f"storage:{filename}", mtime_ms, mtime_ms))
    return attach_id


# ---------------------------------------------------------------------------
# PDF download with priority order
# ---------------------------------------------------------------------------

def ads_gateway_download(bibcode, dest):
    """
    Download via ADS link_gateway with the ADS auth token.
    The gateway authenticates the redirect chain, bypassing publisher bot protection
    (tested: works for IOPscience/ApJ and Oxford/MNRAS which block unauthenticated curl).
    """
    import urllib.request as _ur, urllib.parse as _up
    tok = ads_token()
    if not tok or not bibcode:
        return False
    url = f"https://ui.adsabs.harvard.edu/link_gateway/{_up.quote(bibcode, safe='')}/PUB_PDF"
    try:
        req = _ur.Request(url, headers={
            "Authorization": f"Bearer {tok}",
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/125.0.0.0 Safari/537.36"),
        })
        with _ur.urlopen(req, timeout=60) as r:
            with open(dest, "wb") as f:
                f.write(r.read())
        return is_pdf(dest)
    except Exception:
        try: os.remove(dest)
        except OSError: pass
        return False


_BROWSER_MOD = None
def browser_fetch_pdf(doi, bibcode, dest, verbose=False):
    """Version-of-record PDF via the user's own browser session (configurable;
    see browser_fetch.py). Returns (ok, label); (False, None) if unavailable."""
    global _BROWSER_MOD
    if _BROWSER_MOD is None:
        import importlib.util
        p = os.path.join(os.path.dirname(__file__), "browser_fetch.py")
        spec = importlib.util.spec_from_file_location("browser_fetch", p)
        _BROWSER_MOD = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_BROWSER_MOD)
    return _BROWSER_MOD.fetch_via_browser(doi, bibcode, dest, verbose=verbose)


def annas_archive_pdf(doi, dest, verbose=False):
    """Fetch PDF from Anna's Archive SciDB mirror for a given DOI.
    Fetches https://annas-archive.gl/scidb/<doi>/, tries each .pdf href in order
    (primary CDN first, then IPFS fallback). Reliable for paywalled papers ≤ 2024."""
    import urllib.request as _ur
    if not doi:
        return False, None
    url = f"https://annas-archive.gl/scidb/{doi}/"
    try:
        req = _ur.Request(url, headers={
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/125.0.0.0 Safari/537.36"),
        })
        with _ur.urlopen(req, timeout=20) as r:
            html = r.read().decode("utf-8", errors="replace")
        pdf_urls = re.findall(r'href="(https?://[^"]+\.pdf[^"]*)"', html)
        if not pdf_urls:
            return False, None
        for pdf_url in pdf_urls:
            if verbose:
                print(f"    annas-archive: {pdf_url[:100]}...")
            try:
                if download(pdf_url, dest) and is_pdf(dest):
                    return True, "Anna's Archive"
            except Exception:
                pass
            try: os.remove(dest)
            except OSError: pass
    except Exception:
        pass
    return False, None


def fetch_pdf(arxiv, doi, bibcode, dest, year=None):
    """
    Try to download the best available PDF to dest. Returns (success, source_label).

    Priority:
      1. ADS link_gateway (authenticated) — bypasses IOPscience/Oxford bot protection
      2. ADS resolver  — PUB_PDF / AUTHOR_PDF / ADS_PDF / ADS_SCAN (direct URLs)
      3. Unpaywall     — open-access publisher copy
      4. Authenticated browser (version of record via Firefox session)
      5. Anna's Archive SciDB mirror — for papers published ≤ 2024
      6. ADS resolver  — EPRINT_PDF (arXiv via ADS)
      7. Direct arXiv  — fallback
    """
    priority_urls, eprint_url = ads_esource_urls(bibcode)

    # 1. ADS authenticated gateway (works for IOPscience, Oxford, etc.)
    if bibcode:
        try:
            if ads_gateway_download(bibcode, dest):
                return True, "ADS-gateway"
        except Exception:
            try: os.remove(dest)
            except OSError: pass

    # 2. ADS non-preprint direct URLs
    for url in priority_urls:
        try:
            if download(url, dest) and is_pdf(dest):
                return True, "ADS"
        except Exception:
            try: os.remove(dest)
            except OSError: pass

    # 3. Unpaywall
    if doi:
        u = unpaywall_pdf(doi)
        if u:
            try:
                if download(u, dest) and is_pdf(dest):
                    return True, "Unpaywall"
            except Exception:
                try: os.remove(dest)
                except OSError: pass

    # 4. Authenticated browser (version of record via the user's Firefox session) —
    #     before the preprint fallbacks so we prefer the published PDF over arXiv.
    ok, src = browser_fetch_pdf(doi, bibcode, dest)
    if ok:
        return True, src

    # 5. Anna's Archive SciDB mirror — reliable for paywalled papers ≤ 2024
    if doi and (year is None or year <= 2024):
        ok, src = annas_archive_pdf(doi, dest)
        if ok:
            return True, src

    # 6. ADS arXiv (EPRINT_PDF)
    if eprint_url:
        try:
            if download(eprint_url, dest) and is_pdf(dest):
                return True, "ADS/arXiv"
        except Exception:
            try: os.remove(dest)
            except OSError: pass

    # 7. Direct arXiv
    if arxiv:
        try:
            if download(f"https://arxiv.org/pdf/{arxiv}.pdf", dest) and is_pdf(dest):
                return True, "arXiv"
        except Exception:
            try: os.remove(dest)
            except OSError: pass

    return False, None


# ---------------------------------------------------------------------------
# Main fetch loop
# ---------------------------------------------------------------------------

def fetch_missing_pdfs(dry_run=False, limit=None, verbose=False, only=None):
    pa = sqlite3.connect(DB);        pa.row_factory = sqlite3.Row
    zc = sqlite3.connect(ZOTERO_DB); zc.row_factory = sqlite3.Row

    # Ensure bibcode column exists (idempotent)
    try:
        pa.execute("ALTER TABLE papers ADD COLUMN bibcode TEXT")
        pa.commit()
    except sqlite3.OperationalError:
        pass

    candidates = pa.execute("""
        SELECT id, citekey, title, authors, year, doi, arxiv, bibcode
        FROM papers
        WHERE pdf_path IS NULL
          AND (doi IS NOT NULL OR arxiv IS NOT NULL)
        ORDER BY year DESC
    """).fetchall()

    if only is not None:
        candidates = [r for r in candidates
                      if str(r["id"]) == str(only) or r["citekey"] == only]

    print(f"Found {len(candidates)} papers missing a PDF")
    if not candidates:
        pa.close(); zc.close(); return

    os.makedirs(PDFS_DIR, exist_ok=True)
    stats = {"linked": 0, "fetched_zotero": 0, "fetched_fallback": 0,
             "failed": 0, "skipped": 0}
    touched_lists = set()

    for row in candidates:
        if limit and (stats["fetched_zotero"] + stats["fetched_fallback"]) >= limit:
            break

        pid     = row["id"]
        doi     = _norm_doi(row["doi"])
        arxiv   = _extract_arxiv(row["arxiv"], row["doi"])
        citekey = row["citekey"]
        title   = row["title"] or ""
        authors = row["authors"] or ""
        year    = row["year"]
        label   = f"[{citekey or pid}] {title[:55]}"

        if dry_run:
            zp_id, _ = find_zotero_parent(zc, doi, arxiv, citekey)
            dest_type = "zotero" if zp_id else "fallback"
            print(f"  {'✓' if zp_id else '~'} {label}  → {dest_type}")
            stats["skipped"] += 1
            continue

        zp_id, _ = find_zotero_parent(zc, doi, arxiv, citekey)

        if zp_id:
            # ── Check Zotero for an existing PDF attachment ──────────────────
            existing = existing_zotero_pdf(zc, zp_id)
            if existing:
                pa.execute("UPDATE papers SET pdf_path=?, pdf_on_disk=1 WHERE id=?",
                           (existing, pid))
                for r in pa.execute(
                    "SELECT l.name FROM lists l JOIN paper_lists pl "
                    "ON pl.list_id=l.id WHERE pl.paper_id=?", (pid,)):
                    touched_lists.add(r["name"])
                stats["linked"] += 1
                if verbose: print(f"  → {label}\n    already in Zotero: {existing}")
                continue

            # ── Look up ADS bibcode; write back to Zotero if new ─────────────
            bibcode = row["bibcode"] or zotero_bibcode_in_extra(zc, zp_id)
            if not bibcode:
                bibcode = ads_lookup_bibcode(doi, arxiv)
                if bibcode:
                    write_zotero_bibcode(zc, zp_id, bibcode)
                    pa.execute("UPDATE papers SET bibcode=? WHERE id=?", (bibcode, pid))
                    if verbose: print(f"  ℹ {label}\n    bibcode: {bibcode}")

            # ── Download ──────────────────────────────────────────────────────
            storage_key  = new_zotero_key(zc)
            fname        = zotero_filename(authors, year, title)
            storage_path = os.path.join(STORAGE_DIR, storage_key)
            os.makedirs(storage_path, exist_ok=True)
            dest         = os.path.join(storage_path, fname)

            if verbose:
                src = f"bibcode={bibcode}" if bibcode else (f"doi={doi}" if doi else f"arxiv={arxiv}")
                print(f"  ↓ {label} ({src})")

            ok, source = fetch_pdf(arxiv, doi, bibcode, dest, year=year)

            if ok:
                # Strip macOS quarantine (set by Firefox downloads) so Zotero can open the file
                try:
                    subprocess.run(["xattr", "-d", "com.apple.quarantine", dest],
                                   capture_output=True)
                except Exception:
                    pass
                mtime = os.path.getmtime(dest)
                insert_zotero_attachment(zc, zp_id, storage_key, fname, mtime)
                pa.execute("UPDATE papers SET pdf_path=?, pdf_on_disk=1 WHERE id=?",
                           (dest, pid))
                for r in pa.execute(
                    "SELECT l.name FROM lists l JOIN paper_lists pl "
                    "ON pl.list_id=l.id WHERE pl.paper_id=?", (pid,)):
                    touched_lists.add(r["name"])
                stats["fetched_zotero"] += 1
                if verbose: print(f"    ✓ [{source}] → Zotero storage/{storage_key}/")
            else:
                try: os.rmdir(storage_path)
                except OSError: pass
                stats["failed"] += 1
                if verbose: print(f"    ✗ no open-access PDF found")

        else:
            # ── No Zotero parent — fallback to papers-vela/pdfs/ ────────────
            bibcode = row["bibcode"]
            if not bibcode:
                bibcode = ads_lookup_bibcode(doi, arxiv)
                if bibcode:
                    pa.execute("UPDATE papers SET bibcode=? WHERE id=?", (bibcode, pid))

            stem  = f"{last_name(authors)}_{year or 'nd'}"
            kw    = make_keywords(title)
            stem  = (stem + (f"_{kw}" if kw else ""))[:90]
            existing_names = {os.path.basename(r["pdf_path"]) for r in
                              pa.execute("SELECT pdf_path FROM papers WHERE pdf_path IS NOT NULL")}
            fname = stem + ".pdf"
            if fname in existing_names:
                for ch in "abcdefghijklmnopqrstuvwxyz":
                    if stem + ch + ".pdf" not in existing_names:
                        fname = stem + ch + ".pdf"; break
            dest = os.path.join(PDFS_DIR, fname)

            if verbose: print(f"  ~ {label}  (not in Zotero → fallback)")

            ok, source = fetch_pdf(arxiv, doi, bibcode, dest, year=year)
            if ok:
                pa.execute("UPDATE papers SET pdf_path=?, pdf_on_disk=1 WHERE id=?",
                           (dest, pid))
                for r in pa.execute(
                    "SELECT l.name FROM lists l JOIN paper_lists pl "
                    "ON pl.list_id=l.id WHERE pl.paper_id=?", (pid,)):
                    touched_lists.add(r["name"])
                stats["fetched_fallback"] += 1
                if verbose: print(f"    ✓ [{source}] → pdfs/")
            else:
                stats["failed"] += 1
                if verbose: print(f"    ✗ no open-access PDF found")

    zc.commit()
    pa.commit()

    print(f"\nDone:"
          f"  {stats['linked']} already in Zotero (linked)"
          f"  |  {stats['fetched_zotero']} downloaded to Zotero storage"
          f"  |  {stats['fetched_fallback']} downloaded to fallback pdfs/"
          f"  |  {stats['failed']} unavailable")

    changed = stats["fetched_zotero"] + stats["fetched_fallback"] + stats["linked"]
    if changed and not dry_run:
        n = reindex_lists(pa, list(touched_lists) if touched_lists else None)
        pa.commit()
        print(f"Reindexed {n} lists")
        gen = os.path.join(PAPERS_VELA, "generate_outputs.py")
        import subprocess as sp
        r = sp.run([sys.executable, gen],
                   env={**os.environ, "PAPERS_LIB": LIB},
                   capture_output=True, text=True)
        if r.returncode == 0:
            print("references.bib + INDEX.md regenerated")
        else:
            print(f"bib warning: {r.stderr.strip()}")

    pa.close()
    zc.close()
    return stats


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run",  action="store_true")
    ap.add_argument("--limit",    type=int, default=None)
    ap.add_argument("--verbose",  action="store_true")
    ap.add_argument("--id", dest="only", default=None,
                    help="fetch only this paper (papers-vela id or citekey)")
    ap.add_argument("--no-check", action="store_true",
                    help="skip Zotero-running check (dangerous)")
    a = ap.parse_args()
    require_lib()

    if not os.path.exists(DB):
        sys.exit(f"papers-vela library not found at {DB}")
    if not os.path.exists(ZOTERO_DB):
        sys.exit(f"Zotero database not found at {ZOTERO_DB}")

    if not a.dry_run and not a.no_check:
        if zotero_is_running():
            sys.exit(
                "⚠  Zotero is currently running.\n"
                "   Close Zotero first, then re-run this command.\n"
                "   (Use --no-check to override — at your own risk.)")

    fetch_missing_pdfs(dry_run=a.dry_run, limit=a.limit, verbose=a.verbose, only=a.only)


if __name__ == "__main__":
    main()
