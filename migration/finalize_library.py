#!/usr/bin/env python3
"""
Finalize the PDF library on INTERNAL Nextcloud (target chosen: 18 GB free & stable).

For every paper with a PDF (existing local pdf_path, or a gap match harvested to FPOD19),
compute a homogenized name  <LastName>_<Year>[a|b|c]_<2-3 title keywords>.pdf  and copy it
into a fresh staging dir, then atomically swap it in as ~/Nextcloud/Papers/pdfs. Build
by-list/<List>/ symlink folders so each paper shows up under every list it belongs to.

FPOD19 must be mounted (source of the 317 gap PDFs). Existing internal PDFs are copied
(not moved) into staging, so the originals stay intact until the final swap.

Default = DRY RUN. Pass --apply to perform it.
"""
import os, re, json, shutil, sqlite3, sys, unicodedata

NEXT = os.path.expanduser("~/Nextcloud/Papers")
DB = os.path.join(NEXT, "library.db")
OLD_PDFS = os.path.join(NEXT, "pdfs")
STAGE = os.path.join(NEXT, "pdfs_new")
BYLIST = os.path.join(NEXT, "by-list")
GAP_MAP = "/Volumes/FPOD19/papers_scratch/gap_pdf_matches.json"
APPLY = "--apply" in sys.argv

STOP = {"the","a","an","of","in","on","and","or","for","to","with","from","by","at","as",
        "is","are","be","this","that","their","its","we","our","using","use","via","new",
        "i","ii","iii"}


def ascii_fold(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))


def last_name(authors):
    a = (authors or "").strip()
    if not a:
        return "anon"
    before_comma = a.split(",")[0]
    if "," in a and " " not in before_comma.strip():
        ln = before_comma                  # bibtex "Last, First ..."
    else:                                  # "First Last" (papers3) or single name
        first_author = a.split(" and ")[0].split(",")[0].strip()
        toks = first_author.split()
        ln = toks[-1] if toks else first_author
    ln = re.sub(r"[^A-Za-z]", "", ascii_fold(ln))
    return ln or "anon"


def title_keywords(title, n=3):
    t = ascii_fold(title or "")
    t = re.sub(r"\$[^$]*\$", " ", t)
    t = re.sub(r"\\[A-Za-z]+", " ", t)
    t = re.sub(r"[{}^_]", " ", t)
    out = []
    for w in re.findall(r"[A-Za-z][A-Za-z-]{2,}", t.lower()):
        w = w.strip("-")
        if len(w) >= 3 and w not in STOP:
            out.append(w)
        if len(out) >= n:
            break
    return "-".join(out)


def main():
    fpod = os.path.ismount("/Volumes/FPOD19")
    if not fpod:
        print("WARNING: FPOD19 not mounted — the 317 gap PDFs won't be available.")
        if APPLY:
            sys.exit("Reconnect FPOD19 before --apply (otherwise gaps would be silently dropped).")
    db = sqlite3.connect(DB); db.row_factory = sqlite3.Row
    gap = {int(k): v for k, v in json.load(open(GAP_MAP)).items()} if os.path.exists(GAP_MAP) else {}

    rows = db.execute("SELECT id,authors,year,title,pdf_path FROM papers").fetchall()
    plan = []          # (id, src, lastname, year, title, is_gap)
    for r in rows:
        src, is_gap = None, False
        if r["pdf_path"]:
            c = os.path.join(NEXT, r["pdf_path"])
            if os.path.exists(c):
                src = c
        if src is None and r["id"] in gap and os.path.exists(gap[r["id"]]):
            src, is_gap = gap[r["id"]], True
        if src:
            plan.append((r["id"], src, last_name(r["authors"]), r["year"], r["title"], is_gap))

    groups = {}
    for pid, src, ln, yr, title, isg in plan:
        groups.setdefault((ln, yr), []).append((pid, src, title, isg))
    final = {}         # pid -> (name, src, is_gap)
    for (ln, yr), items in groups.items():
        items.sort(key=lambda x: ((x[2] or ""), x[0]))
        multi = len(items) > 1
        for idx, (pid, src, title, isg) in enumerate(items):
            letter = chr(ord("a") + idx) if multi else ""
            kw = title_keywords(title)
            stem = f"{ln}_{yr or 'nd'}{letter}" + (f"_{kw}" if kw else "")
            final[pid] = (stem[:90] + ".pdf", src, isg)

    ngap = sum(1 for v in final.values() if v[2])
    print("=" * 60); print("APPLY" if APPLY else "DRY RUN"); print("=" * 60)
    print(f"papers with a PDF to place: {len(final)}  ({len(final)-ngap} existing + {ngap} from gap harvest)")
    print("sample names:")
    for name, _, _ in list(final.values())[:10]:
        print("   ", name)
    if not APPLY:
        print("\n(dry run — rerun with --apply)")
        return

    # ---- copy into fresh staging dir ----
    if os.path.exists(STAGE):
        shutil.rmtree(STAGE)
    os.makedirs(STAGE)
    placed = gapdone = failed = 0
    for pid, (name, src, isg) in final.items():
        dest = os.path.join(STAGE, name)
        try:
            if not os.path.exists(dest):
                shutil.copy2(src, dest)
            db.execute("UPDATE papers SET pdf_path=? WHERE id=?", (f"pdfs/{name}", pid))
            placed += 1; gapdone += 1 if isg else 0
        except OSError as e:
            failed += 1; print(f"   FAIL id={pid}: {e}")
    db.commit()
    print(f"staged: {placed} (gap {gapdone}), failed {failed}")

    # ---- atomic-ish swap: pdfs -> pdfs_old_bak, pdfs_new -> pdfs ----
    bak = OLD_PDFS + "_old"
    if os.path.exists(OLD_PDFS):
        if os.path.exists(bak):
            shutil.rmtree(bak)
        os.rename(OLD_PDFS, bak)
    os.rename(STAGE, OLD_PDFS)
    print(f"swapped in new pdfs/; previous originals kept at {bak} (delete after verify)")

    # ---- by-list symlink tree ----
    if os.path.isdir(BYLIST):
        shutil.rmtree(BYLIST)
    os.makedirs(BYLIST)
    nl = nlink = 0
    for l in db.execute("SELECT id,name,source FROM lists"):
        members = db.execute(
            "SELECT p.pdf_path FROM paper_lists pl JOIN papers p ON p.id=pl.paper_id "
            "WHERE pl.list_id=? AND p.pdf_path IS NOT NULL", (l["id"],)).fetchall()
        if not members:
            continue
        tag = "" if l["source"] == "cloud" else " (legacy)" if l["source"] == "legacy" else ""
        d = os.path.join(BYLIST, l["name"].replace("/", "_") + tag)
        os.makedirs(d, exist_ok=True); nl += 1
        for m in members:
            fn = os.path.basename(m["pdf_path"])
            link = os.path.join(d, fn)
            if not os.path.lexists(link):
                os.symlink(os.path.join("..", "..", "pdfs", fn), link); nlink += 1
    print(f"by-list: {nl} list folders, {nlink} symlinks")
    tot = db.execute("SELECT count(*) FROM papers WHERE pdf_path IS NOT NULL").fetchone()[0]
    print(f"papers with pdf_path: {tot}")


if __name__ == "__main__":
    main()
