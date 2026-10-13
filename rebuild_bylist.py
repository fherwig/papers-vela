#!/usr/bin/env python3
"""Rebuild $PAPERS_LIB/by-list/ symlink tree from library.db (fast; no PDF copying).
Run after any list-membership change."""
import os, shutil, sqlite3, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from papers import require_lib, LIB   # library guard + the resolved library

NEXT = LIB   # resolved like every other command: $PAPERS_LIB, then the profile, then defaults
DB = os.path.join(NEXT, "library.db")
BYLIST = os.path.join(NEXT, "by-list")


def main():
    require_lib()
    db = sqlite3.connect(DB); db.row_factory = sqlite3.Row
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
        tag = "" if l["source"] == "cloud" else (" (legacy)" if l["source"] == "legacy" else " (manual)")
        d = os.path.join(BYLIST, l["name"].replace("/", "_") + tag)
        os.makedirs(d, exist_ok=True); nl += 1
        for m in members:
            fn = os.path.basename(m["pdf_path"])
            link = os.path.join(d, fn)
            if not os.path.lexists(link):
                os.symlink(os.path.join("..", "..", "pdfs", fn), link); nlink += 1
    print(f"by-list rebuilt: {nl} folders, {nlink} symlinks")


if __name__ == "__main__":
    main()
