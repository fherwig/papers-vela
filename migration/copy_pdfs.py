#!/usr/bin/env python3
"""
Copy the local-trove PDFs referenced by library.db into ~/Nextcloud/Papers/pdfs/
with clean, stable names; dedupe by source path; update papers.pdf_path.

Non-destructive: reads from the trove, never modifies it. Idempotent: skips a
destination that already exists with the same size, so it is safe to re-run.
"""
import os, re, shutil, sqlite3, sys

HOME = os.path.expanduser("~")
TROVE = os.path.join(HOME, "Workshop/Library/Library.papers3")
LIB = os.path.join(HOME, "Nextcloud/Papers")
DB = os.path.join(LIB, "library.db")
PDFS = os.path.join(LIB, "pdfs")
LOG = os.path.join(LIB, "pdf_copy.log")


def sanitize(s):
    s = (s or "").strip()
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s)
    return s.strip("._-")[:80]


def basename_for(row):
    base = sanitize(row["citekey"])
    if not base:
        au = (row["authors"] or "").split(",")[0].split()
        last = au[-1] if au else "anon"
        base = sanitize(f"{last}{row['year'] or ''}")
    if not base:
        base = f"paper{row['id']}"
    return base


def main():
    os.makedirs(PDFS, exist_ok=True)
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT id, citekey, authors, year, pdf_src FROM papers "
        "WHERE pdf_on_disk=1 AND pdf_src IS NOT NULL ORDER BY id"
    ).fetchall()

    src_to_dest = {}          # dedupe identical source files
    used = set()
    copied = skipped = errors = 0
    bytes_copied = 0
    log = open(LOG, "w")

    def out(msg):
        print(msg)
        log.write(msg + "\n")

    out(f"Copying PDFs for {len(rows)} papers -> {PDFS}")
    for r in rows:
        src = os.path.join(TROVE, r["pdf_src"])
        if not os.path.exists(src):
            errors += 1
            out(f"  MISSING src for id={r['id']}: {r['pdf_src']}")
            continue
        if r["pdf_src"] in src_to_dest:
            dest_rel = src_to_dest[r["pdf_src"]]            # reuse same copy
        else:
            base = basename_for(r)
            name = base + ".pdf"
            if name.lower() in used:                         # disambiguate collisions
                name = f"{base}_{r['id']}.pdf"
            used.add(name.lower())
            dest_rel = os.path.join("pdfs", name)
            dest = os.path.join(LIB, dest_rel)
            try:
                if os.path.exists(dest) and os.path.getsize(dest) == os.path.getsize(src):
                    skipped += 1
                else:
                    shutil.copy2(src, dest)
                    copied += 1
                    bytes_copied += os.path.getsize(dest)
                src_to_dest[r["pdf_src"]] = dest_rel
            except OSError as e:
                errors += 1
                out(f"  ERROR id={r['id']}: {e}")
                continue
        db.execute("UPDATE papers SET pdf_path=? WHERE id=?", (dest_rel, r["id"]))

    db.commit()
    n_with_path = db.execute("SELECT count(*) FROM papers WHERE pdf_path IS NOT NULL").fetchone()[0]
    n_unique = len(set(src_to_dest.values()))
    out("")
    out(f"DONE: copied={copied}, skipped(existing)={skipped}, errors={errors}")
    out(f"unique PDF files in pdfs/: {n_unique}")
    out(f"papers now with pdf_path: {n_with_path}")
    out(f"bytes copied this run: {bytes_copied/1e6:.1f} MB")
    log.close()
    db.close()


if __name__ == "__main__":
    main()
