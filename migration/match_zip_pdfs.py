#!/usr/bin/env python3
"""
Match the PDFs in the per-list zips (extracted on FPOD19) to the current-library
papers that still lack a local PDF (in_cloud=1 AND pdf_path IS NULL).

In-zip filenames are useless (Journal-Year.pdf), so we read each PDF's first pages
with pdftotext and match a paper by its known DOI / arXiv id appearing in the text.
Search is scoped to the paper's OWN cloud-list folders to avoid cross-list mistakes.

Read-only: builds a text cache + a gap->source-pdf map on FPOD19, reports counts and
the total size to copy. Does NOT copy anything (that's the disk-aware next step).
"""
import os, re, json, subprocess, sqlite3

SCRATCH = "/Volumes/FPOD19/papers_scratch"
EXTRACTED = os.path.join(SCRATCH, "extracted")
TXT_CACHE = os.path.join(SCRATCH, "pdf_text_cache.json")
OUT_MAP = os.path.join(SCRATCH, "gap_pdf_matches.json")
DB = os.path.expanduser("~/Nextcloud/Papers/library.db")


def alnum(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def folder_for(listname):
    return listname.replace("/", "_")


def first_page_text(path):
    try:
        r = subprocess.run(["pdftotext", "-f", "1", "-l", "2", path, "-"],
                           capture_output=True, text=True, timeout=40)
        return r.stdout[:6000]
    except Exception:
        return ""


def main():
    # ---- build / load text cache for every extracted PDF ----
    all_pdfs = []
    for root, _, files in os.walk(EXTRACTED):
        for fn in files:
            if fn.lower().endswith(".pdf"):
                all_pdfs.append(os.path.join(root, fn))
    cache = {}
    if os.path.exists(TXT_CACHE):
        cache = json.load(open(TXT_CACHE))
    todo = [p for p in all_pdfs if p not in cache]
    print(f"PDFs: {len(all_pdfs)}; extracting text for {len(todo)} new (cached {len(cache)})")
    for i, p in enumerate(todo, 1):
        cache[p] = alnum(first_page_text(p))
        if i % 100 == 0:
            print(f"  ...{i}/{len(todo)}")
    json.dump(cache, open(TXT_CACHE, "w"))

    # index normalized text by folder for scoped search
    by_folder = {}
    for p, t in cache.items():
        fold = os.path.basename(os.path.dirname(p))
        by_folder.setdefault(fold, []).append((p, t))

    # ---- gap papers + their cloud lists ----
    db = sqlite3.connect(DB); db.row_factory = sqlite3.Row
    gaps = db.execute(
        "SELECT id,doi,arxiv,title,year,journal FROM papers "
        "WHERE in_cloud=1 AND pdf_path IS NULL").fetchall()
    paper_lists = {}
    for r in db.execute(
        "SELECT pl.paper_id pid, l.name FROM paper_lists pl JOIN lists l ON l.id=pl.list_id "
        "WHERE l.source='cloud'"):
        paper_lists.setdefault(r["pid"], []).append(r["name"])

    matches = {}; by_doi = by_arxiv = 0
    unmatched = []
    used = set()
    for g in gaps:
        needle_doi = alnum(g["doi"]) if g["doi"] else None
        needle_ax = alnum(g["arxiv"]) if g["arxiv"] else None
        folders = [folder_for(n) for n in paper_lists.get(g["id"], [])]
        cands = []
        for f in folders:
            cands += by_folder.get(f, [])
        hit = None; how = None
        for p, t in cands:
            if p in used:
                continue
            if needle_doi and len(needle_doi) >= 8 and needle_doi in t:
                hit, how = p, "doi"; break
            if needle_ax and len(needle_ax) >= 6 and needle_ax in t:
                hit, how = p, "arxiv"; break
        if hit:
            matches[g["id"]] = hit
            used.add(hit)
            if how == "doi": by_doi += 1
            else: by_arxiv += 1
        else:
            unmatched.append(g)

    json.dump(matches, open(OUT_MAP, "w"))
    size = sum(os.path.getsize(p) for p in matches.values())
    print("\n" + "=" * 56)
    print(f"gap papers (current library, no local PDF): {len(gaps)}")
    print(f"  matched a zip PDF: {len(matches)}  (by doi={by_doi}, by arxiv={by_arxiv})")
    print(f"  UNMATCHED: {len(unmatched)}")
    print(f"  total size to copy if we take them: {size/1e6:.0f} MB")
    nodoi = sum(1 for g in unmatched if not g["doi"] and not g["arxiv"])
    print(f"  (of unmatched, {nodoi} have no DOI/arXiv at all -> need filename/title fallback or manual)")
    print(f"\nwrote map -> {OUT_MAP}")
    print("sample unmatched (year | journal | title):")
    for g in unmatched[:10]:
        print(f"   {g['year']} | {(g['journal'] or '')[:18]:<18} | {(g['title'] or '')[:50]}")


if __name__ == "__main__":
    main()
