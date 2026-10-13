#!/usr/bin/env python3
"""
Ingest the per-list cloud BibTeX exports (~/Downloads/<list>.bib) into library.db.

- Each file name = a ReadCube list; entries in it = that list's CURRENT membership.
- Match each cloud entry to an existing paper by citekey -> doi -> arxiv -> title+year.
- Matched: enrich missing fields (abstract, doi, arxiv, keywords); add cloud list membership.
- Unmatched: NEW (post-2021) paper -> insert (source='cloud'); flag needs_pdf.
- Cloud lists are canonical; 2021-only lists with no cloud counterpart are kept as legacy.

Default = DRY RUN (reports only). Pass --apply to write changes.
Pass the full cloud list inventory with --inventory to drive rename/legacy logic.
"""
import os, re, sqlite3, glob, sys

HOME = os.path.expanduser("~")
LIB = os.path.join(HOME, "Nextcloud/Papers")
DB = os.path.join(LIB, "library.db")
DOWNLOADS = os.path.join(HOME, "Downloads")
INVENTORY = os.path.join(DOWNLOADS, "ReadCube_Library_Lists.txt")
SKIP_FILES = {"My_Library.bib"}

# explicit 2021 -> cloud renames/merges (case differences handled separately)
RENAME = {
    "binary": "binaries",
    "igw internal gravity waves": "IGWs",
    "pop iii": "Pop III stars",
    "post-agb born-again": "post-AGB",
    "ce & dd merger": "CE & WD merger",
    "neutron star mergers": "neutron star merger/accretion",
    "nova": "nova and rawds",
    "rawd": "nova and rawds",
    "gw190521": "gravitational waves",
    "ligo": "gravitational waves",
    "li rgb": "Li RGB He-core",
    "machine learning": "AIML",
}

APPLY = "--apply" in sys.argv


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def norm_doi(s):
    s = (s or "").strip().lower()
    s = re.sub(r"^https?://(dx\.)?doi\.org/", "", s)
    return s


def split_top(body):
    """split a bibtex entry body on commas at brace depth 0."""
    out, buf, depth = [], [], 0
    for ch in body:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(buf)); buf = []
        else:
            buf.append(ch)
    if "".join(buf).strip():
        out.append("".join(buf))
    return out


def parse_bib(path):
    """yield dicts for each @entry; tolerant brace-matched parser."""
    txt = open(path, encoding="utf-8", errors="replace").read()
    i, n = 0, len(txt)
    entries = []
    while True:
        at = txt.find("@", i)
        if at < 0:
            break
        ob = txt.find("{", at)
        if ob < 0:
            break
        etype = txt[at + 1 : ob].strip().lower()
        # find matching close brace
        depth, j = 0, ob
        while j < n:
            if txt[j] == "{":
                depth += 1
            elif txt[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        body = txt[ob + 1 : j]
        i = j + 1
        if etype in ("comment", "preamble", "string"):
            continue
        parts = split_top(body)
        if not parts:
            continue
        rec = {"_type": etype, "_key": parts[0].strip()}
        for p in parts[1:]:
            if "=" not in p:
                continue
            k, v = p.split("=", 1)
            v = v.strip().strip(",").strip()
            if v.startswith("{") and v.endswith("}"):
                v = v[1:-1]
            elif v.startswith('"') and v.endswith('"'):
                v = v[1:-1]
            rec[k.strip().lower()] = v.strip()
        entries.append(rec)
    return entries


def main():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row

    # --- existing papers indexes ---
    by_citekey, by_doi, by_arxiv, by_titley = {}, {}, {}, {}
    for r in db.execute("SELECT id,citekey,doi,arxiv,title,year,abstract FROM papers"):
        if r["citekey"]:
            by_citekey[norm(r["citekey"])] = r["id"]
        if r["doi"]:
            by_doi[norm_doi(r["doi"])] = r["id"]
        if r["arxiv"]:
            by_arxiv[r["arxiv"].strip().lower()] = r["id"]
        by_titley.setdefault((norm(r["title"]), r["year"]), r["id"])

    def match(rec):
        k = norm(rec.get("_key"))
        if k in by_citekey:
            return by_citekey[k], "citekey"
        d = norm_doi(rec.get("doi"))
        if d and d in by_doi:
            return by_doi[d], "doi"
        a = (rec.get("eprint") or rec.get("arxiv") or "").strip().lower()
        if a and a in by_arxiv:
            return by_arxiv[a], "arxiv"
        try:
            y = int(rec.get("year", "") or 0) or None
        except ValueError:
            y = None
        t = (norm(rec.get("title")), y)
        if t[0] and t in by_titley:
            return by_titley[t], "title"
        return None, None

    # --- cloud inventory (full list set) for canonical names + rename/legacy logic ---
    inv = []
    for invpath in (INVENTORY, os.path.join(LIB, "cloud_lists.txt")):
        if os.path.exists(invpath):
            for line in open(invpath):
                m = re.match(r"\s*\d+\.\s+(.*\S)", line)
                if m:
                    inv.append(m.group(1).strip())
            break
    inv_norm = {norm(x): x for x in inv}
    # safety: without a real inventory we cannot tell cloud lists from legacy ones,
    # so a partial bib set would mis-flip every list. Refuse to write in that case.
    if APPLY and len(inv) < 10:
        sys.exit(f"Refusing to --apply: cloud-list inventory missing/too small ({len(inv)}). "
                 f"Restore ~/Nextcloud/Papers/cloud_lists.txt before re-running.")

    # --- read per-list bib files; canonicalize filename->inventory name (handles '/'->'_') ---
    files = [f for f in sorted(glob.glob(os.path.join(DOWNLOADS, "*.bib")))
             if os.path.basename(f) not in SKIP_FILES]
    cloud_lists = {}
    for f in files:
        raw = os.path.splitext(os.path.basename(f))[0]
        name = inv_norm.get(norm(raw), raw)
        cloud_lists.setdefault(name, []).extend(parse_bib(f))
    present_cloud = set(cloud_lists)        # canonical cloud names we actually have bibs for

    def cloud_counterpart(old):
        n = norm(old)
        if n in inv_norm:
            return inv_norm[n]
        if old.lower() in RENAME:
            return RENAME[old.lower()]
        return None

    # --- schema helpers ---
    def ensure_column(table, col, decl):
        cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
        if col not in cols:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    ensure_column("papers", "in_cloud", "INTEGER DEFAULT 0")
    ensure_column("lists", "source", "TEXT")

    def reg(pid, rec):                      # keep match indexes current as we insert
        if rec.get("_key"): by_citekey[norm(rec["_key"])] = pid
        d = norm_doi(rec.get("doi")); a = (rec.get("eprint") or "").strip().lower()
        if d: by_doi[d] = pid
        if a: by_arxiv[a] = pid

    def insert_new(rec):
        try: y = int(rec.get("year", "") or 0) or None
        except ValueError: y = None
        cur = db.execute(
            "INSERT INTO papers(citekey,title,authors,year,journal,volume,issue,pages,"
            "doi,arxiv,abstract,tags,is_duplicate,source,in_cloud,pdf_on_disk) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,'cloud',1,0)",
            (rec.get("_key"), rec.get("title"), rec.get("author"), y, rec.get("journal"),
             rec.get("volume"), rec.get("number"), rec.get("pages"),
             (norm_doi(rec.get("doi")) or None), (rec.get("eprint") or None),
             rec.get("abstract"), rec.get("keywords")))
        reg(cur.lastrowid, rec)
        return cur.lastrowid

    def enrich_fields(rec, cur):
        upd = {}
        if (not cur["abstract"]) and rec.get("abstract"): upd["abstract"] = rec["abstract"]
        if (not cur["doi"]) and rec.get("doi"): upd["doi"] = norm_doi(rec["doi"])
        if (not cur["arxiv"]) and rec.get("eprint"): upd["arxiv"] = rec["eprint"]
        if (not cur["tags"]) and rec.get("keywords"): upd["tags"] = rec["keywords"]
        return upd

    # --- dedupe entries across lists; record membership ---
    def entry_key(rec):
        return norm(rec.get("_key")) or norm_doi(rec.get("doi")) or (norm(rec.get("title")) + str(rec.get("year", "")))
    cloud_entries, list_members = {}, {}
    for name, recs in cloud_lists.items():
        for rec in recs:
            k = entry_key(rec)
            cloud_entries.setdefault(k, rec)
            list_members.setdefault(name, set()).add(k)

    # --- resolve each distinct entry: match, or (apply) insert ---
    key_to_pid = {}; n_new = n_matched = n_enrich = 0; match_kinds = {}
    for k, rec in cloud_entries.items():
        mid, kind = match(rec)
        if mid is None:
            n_new += 1
            if APPLY:
                pid = insert_new(rec)
                db.execute("UPDATE papers SET in_cloud=1 WHERE id=?", (pid,))
                key_to_pid[k] = pid
        else:
            n_matched += 1; match_kinds[kind] = match_kinds.get(kind, 0) + 1
            cur = db.execute("SELECT abstract,doi,arxiv,tags FROM papers WHERE id=?", (mid,)).fetchone()
            upd = enrich_fields(rec, cur)
            if upd: n_enrich += 1
            if APPLY:
                if upd:
                    db.execute("UPDATE papers SET %s WHERE id=?" % ",".join(c + "=?" for c in upd),
                               (*upd.values(), mid))
                db.execute("UPDATE papers SET in_cloud=1 WHERE id=?", (mid,))
                key_to_pid[k] = mid

    # --- rebuild lists: cloud canonical (only those we have bibs for); keep the rest ---
    old_rows = db.execute("SELECT id,name FROM lists").fetchall()
    to_delete = [r["id"] for r in old_rows
                 if (cp := cloud_counterpart(r["name"])) is not None and cp in present_cloud]
    legacy_names = sorted(r["name"] for r in old_rows if cloud_counterpart(r["name"]) is None)
    awaiting = sorted(x for x in inv if x not in present_cloud)
    if APPLY:
        db.executemany("UPDATE lists SET source='legacy' WHERE id=?",
                       [(r["id"],) for r in old_rows if cloud_counterpart(r["name"]) is None])
        if to_delete:
            qs = ",".join("?" * len(to_delete))
            db.execute(f"DELETE FROM paper_lists WHERE list_id IN ({qs})", to_delete)
            db.execute(f"DELETE FROM lists WHERE id IN ({qs})", to_delete)
        for name, keys in list_members.items():
            lid = db.execute("INSERT INTO lists(name,source,n_members) VALUES(?,'cloud',?)",
                             (name, len(keys))).lastrowid
            db.executemany("INSERT OR IGNORE INTO paper_lists(paper_id,list_id) VALUES(?,?)",
                           [(key_to_pid[k], lid) for k in keys if k in key_to_pid])
        db.commit()

    # --- report ---
    print("=" * 64); print("APPLIED" if APPLY else "DRY RUN (no changes)"); print("=" * 64)
    print(f"cloud bib files: {len(files)} -> {len(cloud_lists)} lists  (inventory: {len(inv)})")
    if awaiting:
        print(f"  !! inventory lists with NO bib yet (kept as their 2021 version): {awaiting}")
    print(f"distinct cloud entries: {len(cloud_entries)}")
    print(f"  matched existing: {n_matched}  {match_kinds}")
    print(f"  NEW: {n_new}")
    print(f"  enriched (abstract/doi/arxiv/tags): {n_enrich}")
    print(f"  genuinely legacy lists preserved ({len(legacy_names)}): {', '.join(legacy_names)}")
    if APPLY:
        g = lambda q: db.execute(q).fetchone()[0]
        tot = g("SELECT count(*) FROM papers")
        inc = g("SELECT count(*) FROM papers WHERE in_cloud=1")
        ncl = g("SELECT count(*) FROM lists WHERE source='cloud'")
        nlg = g("SELECT count(*) FROM lists WHERE source IS NOT 'cloud'")
        hp = g("SELECT count(*) FROM papers WHERE pdf_path IS NOT NULL")
        gap = g("SELECT count(*) FROM papers WHERE in_cloud=1 AND pdf_path IS NULL")
        print(f"\nAFTER APPLY: papers={tot} (in current cloud={inc}); lists cloud={ncl}, kept/legacy={nlg}")
        print(f"  papers with a local PDF: {hp}")
        print(f"  CURRENT papers still WITHOUT a PDF (download targets): {gap}")
    else:
        print("\n(dry run — rerun with --apply to write)")
    db.close()


if __name__ == "__main__":
    main()
