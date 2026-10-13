#!/usr/bin/env python3
"""
Read-only extractor: Papers 3 SQLite DB  ->  new ~/Nextcloud/Papers/library.db

Pulls live (non-deleted) publications, the lists (collections) + membership, and
maps each publication to its PDF on disk in the local trove. Does NOT modify the
Papers 3 DB and does NOT copy any PDFs (that's a later step) -- it only records the
source path + whether the file exists.

Schema of the new DB:
  papers(id, uuid, citekey, title, authors, year, journal, volume, issue, pages,
         doi, arxiv, abstract, notes, tags, rating, flagged, is_duplicate, added,
         source, pdf_src, pdf_on_disk, pdf_path)
  lists(id, name, ctype, n_members)
  paper_lists(paper_id, list_id)
  papers_fts  -- FTS5 over title/authors/abstract/notes
"""
import os, re, sqlite3, datetime, sys

HOME = os.path.expanduser("~")
SRC_DB = os.path.join(
    HOME,
    "Library/Application Support/Papers3/Library_Data/"
    "C02CVANNML85__CF70F44A-F745-4B56-AA21-B3564BC786F0/"
    "Library.papers3/Database.papersdb",
)
TROVE = os.path.join(HOME, "Workshop/Library/Library.papers3")  # PDF.path is relative to this
OUT_DIR = os.path.join(HOME, "Nextcloud/Papers")
OUT_DB = os.path.join(OUT_DIR, "library.db")
REPORT = os.path.join(OUT_DIR, "coverage_report.md")


def parse_year(pubdate):
    """Papers3 encodes dates like '99201204121200...' -> 99|YYYY|MM|DD|..."""
    if not pubdate:
        return None
    s = str(pubdate)
    if len(s) >= 6 and s[:2] == "99":
        try:
            y = int(s[2:6])
            if 1500 <= y <= 2100:
                return y
        except ValueError:
            pass
    return None


def to_date(ts):
    try:
        return datetime.datetime.utcfromtimestamp(float(ts)).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


def arxiv_from(bundle_string, doi):
    # arXiv ids sometimes live in bundle_string/notes; best-effort
    for src in (bundle_string,):
        if src:
            m = re.search(r"(\d{4}\.\d{4,5}|astro-ph/\d{7}|[a-z\-]+/\d{7})", src)
            if m:
                return m.group(1)
    return None


def main():
    if not os.path.exists(SRC_DB):
        sys.exit(f"Papers 3 DB not found: {SRC_DB}")
    src = sqlite3.connect(f"file:{SRC_DB}?mode=ro&immutable=1", uri=True)
    src.row_factory = sqlite3.Row

    if os.path.exists(OUT_DB):
        os.remove(OUT_DB)
    out = sqlite3.connect(OUT_DB)
    out.row_factory = sqlite3.Row
    out.executescript(
        """
        CREATE TABLE papers(
          id INTEGER PRIMARY KEY,
          uuid TEXT UNIQUE, citekey TEXT, title TEXT, authors TEXT, year INTEGER,
          journal TEXT, volume TEXT, issue TEXT, pages TEXT, doi TEXT, arxiv TEXT,
          abstract TEXT, notes TEXT, tags TEXT, rating INTEGER, flagged INTEGER,
          is_duplicate INTEGER, added TEXT, source TEXT,
          pdf_src TEXT, pdf_on_disk INTEGER, pdf_path TEXT);
        CREATE TABLE lists(id INTEGER PRIMARY KEY, name TEXT, ctype INTEGER, n_members INTEGER);
        CREATE TABLE paper_lists(paper_id INTEGER, list_id INTEGER,
          PRIMARY KEY(paper_id, list_id));
        CREATE INDEX ix_papers_doi ON papers(doi);
        CREATE INDEX ix_papers_year ON papers(year);
        CREATE VIRTUAL TABLE papers_fts USING fts5(
          title, authors, abstract, notes, content='papers', content_rowid='id');
        CREATE TRIGGER papers_ai AFTER INSERT ON papers BEGIN
          INSERT INTO papers_fts(rowid,title,authors,abstract,notes)
          VALUES(new.id,new.title,new.authors,new.abstract,new.notes); END;
        CREATE TRIGGER papers_ad AFTER DELETE ON papers BEGIN
          INSERT INTO papers_fts(papers_fts,rowid,title,authors,abstract,notes)
          VALUES('delete',old.id,old.title,old.authors,old.abstract,old.notes); END;
        CREATE TRIGGER papers_au AFTER UPDATE ON papers BEGIN
          INSERT INTO papers_fts(papers_fts,rowid,title,authors,abstract,notes)
          VALUES('delete',old.id,old.title,old.authors,old.abstract,old.notes);
          INSERT INTO papers_fts(rowid,title,authors,abstract,notes)
          VALUES(new.id,new.title,new.authors,new.abstract,new.notes); END;
        """
    )

    # ---- best primary PDF path per publication (relative to TROVE) ----
    # prefer is_primary, then a pdf mime, then any; require not flagged missing
    pdf_by_pub = {}
    for r in src.execute(
        "SELECT object_id, path, is_primary, mime_type, missing, type "
        "FROM PDF WHERE searchresult<>1 AND path IS NOT NULL"
    ):
        oid = r["object_id"]
        if not oid:
            continue
        path = r["path"]
        on_disk = os.path.exists(os.path.join(TROVE, path))
        score = (1 if (r["is_primary"] or 0) else 0) \
            + (1 if (r["mime_type"] or "").lower().endswith("pdf") else 0) \
            + (2 if on_disk else 0) \
            + (0 if (r["missing"] or 0) else 1)
        prev = pdf_by_pub.get(oid)
        if prev is None or score > prev[0]:
            pdf_by_pub[oid] = (score, path, on_disk)

    # ---- publications (live = not deleted) ----
    uuid_to_id = {}
    n_total = n_dup = 0
    rows = src.execute(
        "SELECT * FROM Publication WHERE searchresult<>1 "
        "AND (marked_deleted IS NULL OR marked_deleted=0)"
    )
    for p in rows:
        n_total += 1
        is_dup = 1 if (p["marked_duplicate"] or 0) else 0
        n_dup += is_dup
        uuid = p["uuid"]
        pdf = pdf_by_pub.get(uuid)
        pages = None
        if p["startpage"]:
            pages = p["startpage"] + (("-" + p["endpage"]) if p["endpage"] else "")
        cur = out.execute(
            "INSERT INTO papers(uuid,citekey,title,authors,year,journal,volume,issue,"
            "pages,doi,arxiv,abstract,notes,tags,rating,flagged,is_duplicate,added,"
            "source,pdf_src,pdf_on_disk,pdf_path) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                uuid,
                p["citekey"],
                p["title"],
                p["full_author_string"] or p["author_string"],
                parse_year(p["publication_date"]),
                p["bundle_string"],
                p["volume"],
                p["number"],
                pages,
                p["doi"],
                arxiv_from(p["bundle_string"], p["doi"]),
                p["summary"],
                p["notes"],
                p["keyword_string"] or p["tag_string"],
                p["rating"],
                p["flagged"],
                is_dup,
                to_date(p["created_at"]) or to_date(p["imported_date"]),
                "papers3",
                pdf[1] if pdf else None,
                1 if (pdf and pdf[2]) else 0,
                None,
            ),
        )
        uuid_to_id[uuid] = cur.lastrowid

    # ---- lists (collections) + membership, only those with live members ----
    coll = {r["uuid"]: (r["name"], r["type"]) for r in src.execute("SELECT uuid,name,type FROM Collection")}
    members = {}  # coll_uuid -> set(paper_id)
    for r in src.execute("SELECT collection, object_id FROM CollectionItem"):
        # skip transient 'temp--...' groups and any id not in the real Collection table
        if r["collection"] not in coll:
            continue
        pid = uuid_to_id.get(r["object_id"])
        if pid is None:
            continue
        members.setdefault(r["collection"], set()).add(pid)

    list_id_map = {}
    for cu, pids in members.items():
        name, ctype = coll.get(cu, ("(unknown)", None))
        cur = out.execute(
            "INSERT INTO lists(name,ctype,n_members) VALUES(?,?,?)",
            (name, ctype, len(pids)),
        )
        list_id_map[cu] = cur.lastrowid
        out.executemany(
            "INSERT OR IGNORE INTO paper_lists(paper_id,list_id) VALUES(?,?)",
            [(pid, cur.lastrowid) for pid in pids],
        )

    out.commit()

    # ---------- coverage report ----------
    def one(q):
        return out.execute(q).fetchone()[0]

    with_doi = one("SELECT count(*) FROM papers WHERE doi IS NOT NULL AND doi<>''")
    with_abs = one("SELECT count(*) FROM papers WHERE abstract IS NOT NULL AND abstract<>''")
    with_pdf = one("SELECT count(*) FROM papers WHERE pdf_on_disk=1")
    pdf_missing = one("SELECT count(*) FROM papers WHERE pdf_src IS NOT NULL AND pdf_on_disk=0")
    no_pdf = one("SELECT count(*) FROM papers WHERE pdf_src IS NULL")
    n_lists = one("SELECT count(*) FROM lists")

    lines = []
    lines.append("# Papers 3 -> library.db -- extraction coverage\n")
    lines.append(f"_Generated from the live (non-deleted) Papers 3 records. Source DB untouched._\n")
    lines.append(f"- **Live publications imported:** {n_total}")
    lines.append(f"  - flagged as duplicates (kept, marked `is_duplicate=1`): {n_dup}")
    lines.append(f"- **With DOI:** {with_doi}  |  with abstract: {with_abs}")
    lines.append(f"- **PDF on disk (resolvable in local trove):** {with_pdf}")
    lines.append(f"- PDF referenced but file missing: {pdf_missing}")
    lines.append(f"- No PDF attached at all: {no_pdf}")
    lines.append(f"- **Lists with >=1 member:** {n_lists}\n")
    lines.append("## Lists (by size)\n")
    lines.append("| List | members | with PDF on disk |")
    lines.append("|------|--------:|-----------------:|")
    for r in out.execute(
        "SELECT l.name, l.n_members, "
        "  SUM(CASE WHEN p.pdf_on_disk=1 THEN 1 ELSE 0 END) AS wp "
        "FROM lists l JOIN paper_lists pl ON pl.list_id=l.id "
        "JOIN papers p ON p.id=pl.paper_id "
        "GROUP BY l.id ORDER BY l.n_members DESC"
    ):
        lines.append(f"| {r['name']} | {r['n_members']} | {r['wp']} |")
    report = "\n".join(lines) + "\n"
    with open(REPORT, "w") as f:
        f.write(report)

    print(report)
    print(f"Wrote {OUT_DB}")
    print(f"Wrote {REPORT}")
    out.close()
    src.close()


if __name__ == "__main__":
    main()
