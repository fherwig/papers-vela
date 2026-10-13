#!/usr/bin/env python3
"""
Place newly-matched gap PDFs into the library (incremental; no full re-finalize).

Reads gap_pdf_matches.json (from match_zip_pdfs.py) and, for every paper that still
lacks a pdf_path but now has a matched source PDF on FPOD19, copies it into pdfs/ with
the homogenized name <LastName>_<Year>[abc]_<keywords>.pdf and updates the DB.

Run after: harvest_pdf_zips.py (+ extract) -> match_zip_pdfs.py.
Then run rebuild_bylist.py and generate_outputs.py.
"""
import os, re, json, shutil, sqlite3, unicodedata

NEXT = os.path.expanduser("~/Nextcloud/Papers")
DB = os.path.join(NEXT, "library.db")
PDFS = os.path.join(NEXT, "pdfs")
GAP_MAP = "/Volumes/FPOD19/papers_scratch/gap_pdf_matches.json"
STOP = {"the","a","an","of","in","on","and","or","for","to","with","from","by","at","as",
        "is","are","be","this","that","their","its","we","our","using","use","via","new","i","ii","iii"}


def fold(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))


def last_name(authors):
    a = (authors or "").strip()
    if not a:
        return "Anon"
    bc = a.split(",")[0]
    if "," in a and " " not in bc.strip():
        ln = bc
    else:
        t = a.split(" and ")[0].split(",")[0].strip().split(); ln = t[-1] if t else "Anon"
    return re.sub(r"[^A-Za-z]", "", fold(ln)) or "Anon"


def keywords(title, n=3):
    t = re.sub(r"\$[^$]*\$", " ", fold(title or ""))
    t = re.sub(r"\\[A-Za-z]+", " ", t); t = re.sub(r"[{}^_]", " ", t)
    out = []
    for w in re.findall(r"[A-Za-z][A-Za-z-]{2,}", t.lower()):
        w = w.strip("-")
        if len(w) >= 3 and w not in STOP:
            out.append(w)
        if len(out) >= n:
            break
    return "-".join(out)


def main():
    if not os.path.exists(GAP_MAP):
        print("no gap map — run match_zip_pdfs.py first"); return
    gap = {int(k): v for k, v in json.load(open(GAP_MAP)).items()}
    db = sqlite3.connect(DB); db.row_factory = sqlite3.Row
    existing = {os.path.basename(r["pdf_path"]) for r in
                db.execute("SELECT pdf_path FROM papers WHERE pdf_path IS NOT NULL")}

    def unique(stem):
        if stem + ".pdf" not in existing:
            existing.add(stem + ".pdf"); return stem + ".pdf"
        for ch in "abcdefghijklmnopqrstuvwxyz":
            if stem + ch + ".pdf" not in existing:
                existing.add(stem + ch + ".pdf"); return stem + ch + ".pdf"
        existing.add(stem + "_x.pdf"); return stem + "_x.pdf"

    placed = 0
    for p in db.execute("SELECT id,authors,year,title FROM papers WHERE pdf_path IS NULL"):
        src = gap.get(p["id"])
        if not src or not os.path.exists(src):
            continue
        stem = f"{last_name(p['authors'])}_{p['year'] or 'nd'}"
        kw = keywords(p["title"]); stem += f"_{kw}" if kw else ""
        name = unique(stem[:90])
        shutil.copy2(src, os.path.join(PDFS, name))
        db.execute("UPDATE papers SET pdf_path=?, pdf_on_disk=1 WHERE id=?", (f"pdfs/{name}", p["id"]))
        placed += 1
    db.commit()
    print(f"placed {placed} newly-matched gap PDFs into pdfs/")
    print(f"papers with PDF now: {db.execute('SELECT count(*) FROM papers WHERE pdf_path IS NOT NULL').fetchone()[0]}")


if __name__ == "__main__":
    main()
