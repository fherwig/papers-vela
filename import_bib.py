#!/usr/bin/env python3
"""
import_bib.py — create/populate a papers-vela library.db from a BibTeX export.

Universal on-ramp: most reference managers (Mendeley, Zotero, ReadCube/Papers, EndNote)
export BibTeX. This creates the schema if needed and inserts entries (dedup by DOI/citekey).

  PAPERS_LIB=~/Papers python3 import_bib.py my_library.bib [--list "Optional list name"]

Then:  papers reindex && papers bib && papers stats
PDFs are not handled here — attach them later via `papers add`/`gaps`, or your own importer.

ADMISSION IS GATED (gate.py). A .bib file is hand-editable text, which makes this the widest
door into the library and the one a wrong entry actually walks through. Every new record is
checked against the authority that issued its identifier BEFORE it is inserted; refusals are
written to a rejects file for review rather than silently dropped or silently admitted.
Use --force-unverified "<reason>" to admit anyway; the reason is recorded per record.
"""
import os, re, sqlite3, sys, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from papers import clean_text, require_lib, LIB, DB   # shared normalization + resolved library path

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers(
  id INTEGER PRIMARY KEY, uuid TEXT, citekey TEXT, title TEXT, authors TEXT, year INTEGER,
  journal TEXT, volume TEXT, issue TEXT, pages TEXT, doi TEXT, arxiv TEXT, abstract TEXT,
  notes TEXT, tags TEXT, rating INTEGER, flagged INTEGER, is_duplicate INTEGER,
  added TEXT, source TEXT, pdf_src TEXT, pdf_on_disk INTEGER, pdf_path TEXT,
  in_cloud INTEGER DEFAULT 1, citation_count INTEGER);
CREATE INDEX IF NOT EXISTS ix_papers_doi ON papers(doi);
CREATE INDEX IF NOT EXISTS ix_papers_year ON papers(year);
CREATE TABLE IF NOT EXISTS lists(id INTEGER PRIMARY KEY, name TEXT UNIQUE, ctype INTEGER,
  n_members INTEGER, source TEXT);
CREATE TABLE IF NOT EXISTS paper_lists(paper_id INTEGER, list_id INTEGER,
  PRIMARY KEY(paper_id, list_id));
CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5(
  title, authors, abstract, notes, content='papers', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS papers_ai AFTER INSERT ON papers BEGIN
  INSERT INTO papers_fts(rowid,title,authors,abstract,notes)
  VALUES(new.id,new.title,new.authors,new.abstract,new.notes); END;
CREATE TRIGGER IF NOT EXISTS papers_ad AFTER DELETE ON papers BEGIN
  INSERT INTO papers_fts(papers_fts,rowid,title,authors,abstract,notes)
  VALUES('delete',old.id,old.title,old.authors,old.abstract,old.notes); END;
CREATE TRIGGER IF NOT EXISTS papers_au AFTER UPDATE ON papers BEGIN
  INSERT INTO papers_fts(papers_fts,rowid,title,authors,abstract,notes)
  VALUES('delete',old.id,old.title,old.authors,old.abstract,old.notes);
  INSERT INTO papers_fts(rowid,title,authors,abstract,notes)
  VALUES(new.id,new.title,new.authors,new.abstract,new.notes); END;
CREATE TABLE IF NOT EXISTS list_summary(name TEXT PRIMARY KEY, blurb TEXT, themes TEXT,
  domains TEXT, n_blurb INTEGER, n_members INTEGER, n_pdf INTEGER, ymin INTEGER, ymax INTEGER,
  top_keywords TEXT, top_authors TEXT, anchors TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS comments(id INTEGER PRIMARY KEY, paper_id INTEGER NOT NULL,
  page INTEGER, quote TEXT, note TEXT, rects TEXT, kind TEXT, created_at TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS ix_comments_paper ON comments(paper_id);
"""


def norm_doi(s):
    s = (s or "").strip().lower()
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", s)


def split_top(body):
    out, buf, depth = [], [], 0
    for ch in body:
        if ch == "{": depth += 1
        elif ch == "}": depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(buf)); buf = []
        else:
            buf.append(ch)
    if "".join(buf).strip():
        out.append("".join(buf))
    return out


def parse_bib(path):
    txt = open(path, encoding="utf-8", errors="replace").read()
    i, n, entries = 0, len(txt), []
    while True:
        at = txt.find("@", i)
        if at < 0: break
        ob = txt.find("{", at)
        if ob < 0: break
        etype = txt[at + 1:ob].strip().lower()
        depth, j = 0, ob
        while j < n:
            if txt[j] == "{": depth += 1
            elif txt[j] == "}":
                depth -= 1
                if depth == 0: break
            j += 1
        body, i = txt[ob + 1:j], j + 1
        if etype in ("comment", "preamble", "string"): continue
        parts = split_top(body)
        if not parts: continue
        rec = {"_type": etype, "_key": parts[0].strip()}
        for p in parts[1:]:
            if "=" not in p: continue
            k, v = p.split("=", 1)
            v = v.strip().strip(",").strip()
            if v[:1] in "{\"" and v[-1:] in "}\"": v = v[1:-1]
            rec[k.strip().lower()] = v.strip()
        entries.append(rec)
    return entries


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bibfile")
    ap.add_argument("--list")
    ap.add_argument("--force-unverified", metavar="REASON", dest="force_unverified",
                    help="admit records the gate refuses; REASON is recorded per record")
    ap.add_argument("--workers", type=int, default=6, help="parallel authority lookups")
    ap.add_argument("--rejects", default=None,
                    help="write refused entries here (default: <bibfile>.rejects.txt)")
    a = ap.parse_args()
    require_lib()
    os.makedirs(LIB, exist_ok=True)
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)

    by_doi = {norm_doi(r["doi"]): r["id"] for r in c.execute("SELECT id,doi FROM papers WHERE doi<>''")}
    by_ck = {(r["citekey"] or "").lower(): r["id"] for r in c.execute("SELECT id,citekey FROM papers WHERE citekey<>''")}

    lid = None
    if a.list:
        row = c.execute("SELECT id FROM lists WHERE name=?", (a.list,)).fetchone()
        lid = row["id"] if row else c.execute(
            "INSERT INTO lists(name,source,n_members) VALUES(?,'import',0)", (a.list,)).lastrowid

    # ---- ADMISSION GATE ---------------------------------------------------------------------
    # Validate every NEW record against its identifier's authority before any insert. Done up
    # front and concurrently so a large import costs one pass of network, not one per row.
    import concurrent.futures
    import gate

    records = list(parse_bib(a.bibfile))
    fresh = [r for r in records
             if (by_doi.get(norm_doi(r.get("doi"))) or by_ck.get((r.get("_key") or "").lower()))
             is None]

    def as_rec(r):
        try: y = int(r.get("year", "") or 0) or None
        except ValueError: y = None
        return {"title": clean_text(r.get("title")), "authors": r.get("author"), "year": y,
                "journal": clean_text(r.get("journal")), "volume": r.get("volume"),
                "pages": r.get("pages"), "doi": r.get("doi"), "arxiv": r.get("eprint")}

    verdicts = {}
    if fresh:
        print(f"gate: checking {len(fresh)} new record(s) against Crossref/arXiv ...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as ex:
            futs = {ex.submit(gate.validate, as_rec(r)): id(r) for r in fresh}
            for f in concurrent.futures.as_completed(futs):
                try:
                    verdicts[futs[f]] = f.result()
                except Exception as e:
                    verdicts[futs[f]] = (False, "unreachable", "none", "", 0.0,
                                         "gate error: %s" % type(e).__name__)

    rejects, refused, overridden = [], 0, 0
    added = dup = 0
    for rec in records:
        d = norm_doi(rec.get("doi"))
        k = (rec.get("_key") or "").lower()
        pid = by_doi.get(d) or by_ck.get(k)
        if pid is None:
            ok, vstatus, vauth, vident, vratio, vdetail = verdicts.get(
                id(rec), (False, "unreachable", "none", "", 0.0, "not checked"))
            if not ok and not a.force_unverified:
                refused += 1
                rejects.append("%-28s %-14s %s\n    title: %s\n    finding: %s\n"
                               % (rec.get("_key") or "?", vstatus, vident or "-",
                                  (clean_text(rec.get("title")) or "")[:100], vdetail[:200]))
                continue                      # REFUSED — does not enter the library
            if not ok:
                overridden += 1
            try: y = int(rec.get("year", "") or 0) or None
            except ValueError: y = None
            cur = c.execute(
                "INSERT INTO papers(citekey,title,authors,year,journal,volume,issue,pages,doi,"
                "arxiv,abstract,tags,source,in_cloud,pdf_on_disk) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'import',1,0)",
                (rec.get("_key"), clean_text(rec.get("title")), rec.get("author"), y,
                 clean_text(rec.get("journal")), rec.get("volume"), rec.get("number"),
                 rec.get("pages"), (d or None), (rec.get("eprint") or None),
                 clean_text(rec.get("abstract")), rec.get("keywords")))
            pid = cur.lastrowid
            gate.record(c, pid, "override" if not ok else vstatus, vauth, vident, vratio,
                        vdetail, override_reason=(a.force_unverified if not ok else None))
            if d: by_doi[d] = pid
            if k: by_ck[k] = pid
            added += 1
        else:
            dup += 1
        if lid:
            c.execute("INSERT OR IGNORE INTO paper_lists(paper_id,list_id) VALUES(?,?)", (pid, lid))
    c.commit()
    total = c.execute("SELECT count(*) FROM papers").fetchone()[0]
    print(f"imported {added} new ({dup} already present) into {DB}; library now {total} papers"
          + (f"; tagged to list '{a.list}'" if a.list else ""))
    if overridden:
        print(f"  ⚠ {overridden} record(s) admitted UNVERIFIED by explicit override "
              f"({a.force_unverified!r}) — recorded; see `papers audit --overrides`")
    if refused:
        path = a.rejects or (a.bibfile + ".rejects.txt")
        with open(path, "w") as fh:
            fh.write("REFUSED BY THE ADMISSION GATE — %d entr(ies)\n"
                     "The authority that issued the identifier disagrees with the entry, or the\n"
                     "identifier does not resolve. A wrong DOI still RESOLVES; it just resolves to\n"
                     "a different paper. Fix the identifier, or re-run with\n"
                     '--force-unverified "<reason>" if the entry genuinely cannot be verified.\n\n'
                     % refused)
            fh.writelines(rejects)
        print(f"  ✗ {refused} entr(ies) REFUSED — not imported. Review: {path}")
    print("next:  papers reindex && papers bib && papers stats")


if __name__ == "__main__":
    main()
