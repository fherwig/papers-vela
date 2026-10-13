#!/usr/bin/env python3
"""
trillium_sync — bidirectional sync of the papers library (library.db + pdfs/)
between this machine's canonical copy and a copy on a remote host reached over SSH (written
for an HPC cluster's /scratch). The remote is the "remote" entry of your profile:
  "remote": {"host": "user@cluster.example.org", "path": "/scratch/user/papers-lib"}

Run from the laptop only (Trillium has no outbound path back here). One call,
fully deterministic, no LLM reasoning needed at merge time:

  - Rows are matched by content identity (doi > arxiv > citekey > normalized
    title+year), never by the raw `id` column. `id` is a local autoincrement
    and is not stable across two independently-writable copies of the DB —
    this is why earlier attempts to share the library file stalled.
  - Conflicts resolve by "coalesce, never clobber": a field only gets filled
    in when one side has it and the other is empty. If both sides set a
    field to different non-empty values, both are left untouched and the
    divergence is reported, not silently resolved.
  - PDFs sync via plain `rsync --ignore-existing` in both directions — safe
    because a given paper's PDF filename is deterministic and files are
    never edited in place once fetched, so "copy whatever's missing" can't
    lose data. Nothing is ever deleted on either side.
  - A cheap fingerprint (row counts + identity keys, no full-content hash) is
    compared first; if it matches on both sides, the whole sync is a no-op —
    the common case costs one local query + one SSH round trip.

Usage:
  python3 trillium_sync.py [--dry-run] [--no-pdfs]
                            [--remote-host HOST] [--remote-dir DIR]
"""
import argparse, os, re, shlex, shutil, sqlite3, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import papers as P

# From the profile, never hard-coded: whose cluster account and which /scratch is personal.
REMOTE_HOST = P.profile_get("remote", "host")
REMOTE_DIR = P.profile_get("remote", "path")

# Columns reconciled on MATCHED rows (present with the same identity on both
# sides): filled in when empty on one side, left alone on real conflicts.
# Excludes uuid/added/source/is_duplicate (provenance of the row's own
# creation event — not meaningful to overwrite across machines).
PLAIN_FIELDS = ["citekey", "title", "authors", "year", "journal", "volume", "issue",
                "pages", "doi", "arxiv", "abstract", "notes", "tags", "rating",
                "flagged", "citation_count", "in_cloud", "pdf_src"]

# Plain printable delimiters, not control chars: char(30)/char(31) do not survive
# identically through every sqlite3 CLI build + subprocess/pty capture chain (observed
# mangled on macOS's system sqlite3 vs Trillium's, causing a false-divergence report).
# "<F>"/"<R>" can't realistically appear inside a DOI/arXiv id/citekey.
FP_SQL = (
    "SELECT (SELECT count(*) FROM papers) || '|' || "
    "(SELECT count(*) FROM papers WHERE pdf_path IS NOT NULL) || '|' || "
    "(SELECT count(*) FROM lists) || '|' || "
    "(SELECT count(*) FROM paper_lists) || '|' || "
    "coalesce((SELECT group_concat(coalesce(doi,'') || '<F>' || coalesce(arxiv,'') || "
    "'<F>' || coalesce(citekey,''), '<R>') FROM "
    "(SELECT doi,arxiv,citekey,title FROM papers ORDER BY coalesce(doi,arxiv,citekey,title))), '');"
)


def empty(v):
    return v is None or (isinstance(v, str) and v.strip() == "")


def norm_doi(s):
    s = (s or "").strip().lower()
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", s)


def norm_title(s):
    return re.sub(r"[^a-z0-9]+", "", P.fold(s or "").lower())


def identity_key(row):
    doi = norm_doi(row["doi"])
    if doi:
        return "doi:" + doi
    ax = (row["arxiv"] or "").strip().lower()
    if ax:
        return "ax:" + ax
    ck = (row["citekey"] or "").strip().lower()
    if ck:
        return "ck:" + ck
    return f"ty:{norm_title(row['title'])}:{row['year'] or ''}"


def run_remote(remote_cmd):
    return subprocess.run(["ssh", REMOTE_HOST, remote_cmd], text=True, capture_output=True)


def rsync(args):
    return subprocess.run(["rsync", "-az", *args], text=True, capture_output=True)


def probe_ssh():
    r = run_remote("echo TRILLIUM_OK")
    if "TRILLIUM_OK" not in r.stdout:
        sys.exit(f"trillium_sync: cannot reach {REMOTE_HOST} over SSH without a prompt.\n"
                 f"If the cluster uses MFA, log in once interactively (ssh {REMOTE_HOST}) to "
                 "open an auth window, then retry.")


def local_fingerprint(path):
    r = subprocess.run(["sqlite3", path, FP_SQL], text=True, capture_output=True)
    return r.stdout.strip()


def remote_fingerprint(remote_dir):
    cmd = f"sqlite3 {shlex.quote(remote_dir + '/library.db')} {shlex.quote(FP_SQL)}"
    return run_remote(cmd).stdout.strip()


def do_merge(canonical_path, remote_tmp_path, dry_run=False):
    conn = sqlite3.connect(canonical_path)
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS remote", (remote_tmp_path,))
    conn.execute("BEGIN")

    cols_no_id = [d[1] for d in conn.execute("PRAGMA table_info(papers)") if d[1] != "id"]
    colnames = ",".join(cols_no_id)
    placeholders = ",".join("?" for _ in cols_no_id)

    local_all = list(conn.execute("SELECT * FROM main.papers"))
    remote_all = list(conn.execute("SELECT * FROM remote.papers"))
    local_rows = {identity_key(r): r for r in local_all}
    remote_rows = {identity_key(r): r for r in remote_all}

    # A row's identity_key is NOT STABLE UNDER ENRICHMENT. It prefers doi > arxiv > citekey, so the
    # moment one copy learns a DOI the other lacks, the same paper keys differently on the two
    # sides, is treated as new, and is inserted -- straight into the papers.uuid UNIQUE constraint.
    # Measured 2026-09-08: 741 of 1110 apparently-new remote rows already existed locally under a
    # different key, and the merge died on the first one.
    #
    # uuid is stable and unique where present, so it settles those cases. It cannot be the primary
    # key: 1679 of 2829 local rows have none. So it is a SECOND matcher, not a replacement.
    local_by_uuid = {r["uuid"]: r for r in local_all if r["uuid"]}
    remote_by_uuid = {r["uuid"]: r for r in remote_all if r["uuid"]}
    rekeyed = 0
    for k, r in list(remote_rows.items()):
        if k not in local_rows and r["uuid"] in local_by_uuid:
            local_rows[k] = local_by_uuid[r["uuid"]]   # same row, different key: pair them
            rekeyed += 1
    for k, l in list(local_rows.items()):
        if k not in remote_rows and l["uuid"] in remote_by_uuid:
            remote_rows[k] = remote_by_uuid[l["uuid"]]
            rekeyed += 1
    if rekeyed:
        print(f"  {rekeyed} row(s) matched by uuid where the content key had diverged")

    stats = {"local_new": 0, "remote_new": 0, "field_fills_local": 0,
             "field_fills_remote": 0, "conflicts": []}

    for key, r in remote_rows.items():
        if key not in local_rows:
            conn.execute(f"INSERT INTO main.papers({colnames}) VALUES({placeholders})",
                         [r[c] for c in cols_no_id])
            stats["local_new"] += 1

    for key, r in local_rows.items():
        if key not in remote_rows:
            conn.execute(f"INSERT INTO remote.papers({colnames}) VALUES({placeholders})",
                         [r[c] for c in cols_no_id])
            stats["remote_new"] += 1

    for key in (set(local_rows) & set(remote_rows)):
        lr, rr = local_rows[key], remote_rows[key]
        local_updates, remote_updates = {}, {}
        for f in PLAIN_FIELDS:
            lv, rv = lr[f], rr[f]
            if empty(lv) and not empty(rv):
                local_updates[f] = rv
            elif empty(rv) and not empty(lv):
                remote_updates[f] = lv
            elif not empty(lv) and not empty(rv) and str(lv) != str(rv):
                stats["conflicts"].append(f"{key}: field '{f}' differs (kept both as-is)")
        lpp, rpp = lr["pdf_path"], rr["pdf_path"]
        if empty(lpp) and not empty(rpp):
            local_updates["pdf_path"], local_updates["pdf_on_disk"] = rpp, 1
        elif empty(rpp) and not empty(lpp):
            remote_updates["pdf_path"], remote_updates["pdf_on_disk"] = lpp, 1
        elif not empty(lpp) and not empty(rpp) and lpp != rpp:
            stats["conflicts"].append(f"{key}: different pdf_path on each side (kept both as-is)")
        if local_updates:
            set_clause = ",".join(f"{k}=?" for k in local_updates)
            conn.execute(f"UPDATE main.papers SET {set_clause} WHERE id=?",
                         (*local_updates.values(), lr["id"]))
            stats["field_fills_local"] += len(local_updates)
        if remote_updates:
            set_clause = ",".join(f"{k}=?" for k in remote_updates)
            conn.execute(f"UPDATE remote.papers SET {set_clause} WHERE id=?",
                         (*remote_updates.values(), rr["id"]))
            stats["field_fills_remote"] += len(remote_updates)

    def current_ids(schema):
        return {identity_key(r): r["id"] for r in conn.execute(f"SELECT * FROM {schema}.papers")}
    local_ids, remote_ids = current_ids("main"), current_ids("remote")

    local_list_names = {r["name"]: r["id"] for r in conn.execute("SELECT id,name FROM main.lists")}
    remote_list_names = {r["name"]: r["id"] for r in conn.execute("SELECT id,name FROM remote.lists")}
    for name in list(remote_list_names):
        if name not in local_list_names:
            cur = conn.execute("INSERT INTO main.lists(name,source,n_members) VALUES(?,?,0)",
                                (name, "synced"))
            local_list_names[name] = cur.lastrowid
    for name in list(local_list_names):
        if name not in remote_list_names:
            cur = conn.execute("INSERT INTO remote.lists(name,source,n_members) VALUES(?,?,0)",
                                (name, "synced"))
            remote_list_names[name] = cur.lastrowid

    def memberships(schema, ids_by_key):
        id_to_key = {v: k for k, v in ids_by_key.items()}
        rows = conn.execute(f"SELECT pl.paper_id AS pid, l.name AS lname FROM {schema}.paper_lists pl "
                             f"JOIN {schema}.lists l ON l.id=pl.list_id").fetchall()
        return {(id_to_key[r["pid"]], r["lname"]) for r in rows if r["pid"] in id_to_key}

    local_mem, remote_mem = memberships("main", local_ids), memberships("remote", remote_ids)
    for key, lname in remote_mem - local_mem:
        pid, lid = local_ids.get(key), local_list_names.get(lname)
        if pid and lid:
            conn.execute("INSERT OR IGNORE INTO main.paper_lists(paper_id,list_id) VALUES(?,?)", (pid, lid))
    for key, lname in local_mem - remote_mem:
        pid, lid = remote_ids.get(key), remote_list_names.get(lname)
        if pid and lid:
            conn.execute("INSERT OR IGNORE INTO remote.paper_lists(paper_id,list_id) VALUES(?,?)", (pid, lid))

    summary_ddl = ("CREATE TABLE IF NOT EXISTS {schema}.list_summary(name TEXT PRIMARY KEY, "
                   "blurb TEXT, themes TEXT, domains TEXT, n_blurb INTEGER, n_members INTEGER, "
                   "n_pdf INTEGER, ymin INTEGER, ymax INTEGER, top_keywords TEXT, top_authors TEXT, "
                   "anchors TEXT, updated_at TEXT)")
    conn.execute(summary_ddl.format(schema="main"))
    conn.execute(summary_ddl.format(schema="remote"))
    local_sum = {r["name"]: r for r in conn.execute("SELECT * FROM main.list_summary")}
    remote_sum = {r["name"]: r for r in conn.execute("SELECT * FROM remote.list_summary")}
    for name in set(local_sum) | set(remote_sum):
        lr, rr = local_sum.get(name), remote_sum.get(name)
        for field in ("blurb", "themes", "domains"):
            lv = lr[field] if lr else None
            rv = rr[field] if rr else None
            if empty(lv) and not empty(rv):
                conn.execute(f"INSERT INTO main.list_summary(name,{field}) VALUES(?,?) "
                             f"ON CONFLICT(name) DO UPDATE SET {field}=excluded.{field}", (name, rv))
            elif empty(rv) and not empty(lv):
                conn.execute(f"INSERT INTO remote.list_summary(name,{field}) VALUES(?,?) "
                             f"ON CONFLICT(name) DO UPDATE SET {field}=excluded.{field}", (name, lv))
            elif not empty(lv) and not empty(rv) and lv != rv:
                stats["conflicts"].append(f"list '{name}': {field} differs (kept both as-is)")

    if dry_run:
        conn.execute("ROLLBACK")
    else:
        conn.execute("COMMIT")
    conn.execute("DETACH remote")
    conn.close()
    return stats


def run(remote_host, remote_dir, dry_run, sync_pdfs):
    global REMOTE_HOST, REMOTE_DIR
    REMOTE_HOST, REMOTE_DIR = remote_host, remote_dir

    probe_ssh()
    canonical = P.DB
    if not os.path.isfile(canonical):
        sys.exit(f"trillium_sync: local library not found at {canonical}")

    run_remote(f"mkdir -p {shlex.quote(remote_dir + '/pdfs')}")
    exists = "EXISTS" in run_remote(
        f"test -f {shlex.quote(remote_dir + '/library.db')} && echo EXISTS || echo MISSING").stdout

    if not exists:
        print(f"no library.db on Trillium at {remote_dir} yet — bootstrapping full push")
        if not dry_run:
            rsync([canonical, f"{remote_host}:{remote_dir}/library.db"])
            if sync_pdfs:
                rsync([P.PDFS + "/", f"{remote_host}:{remote_dir}/pdfs/"])
        suffix = " (dry-run, nothing written)" if dry_run else ""
        print(f"pushed library.db{' + pdfs/' if sync_pdfs else ''} to {remote_host}:{remote_dir}{suffix}")
        return

    local_fp = local_fingerprint(canonical)
    remote_fp = remote_fingerprint(remote_dir)
    if local_fp == remote_fp:
        print("in sync — no changes needed")
        return

    print("divergence detected — merging")
    if not dry_run:
        shutil.copy2(canonical, canonical + ".pre-trillium-sync.bak")

    tmp_remote_db = canonical + ".trillium-tmp"
    rsync([f"{remote_host}:{remote_dir}/library.db", tmp_remote_db])

    if sync_pdfs:
        rsync(["--ignore-existing", P.PDFS + "/", f"{remote_host}:{remote_dir}/pdfs/"])
        rsync(["--ignore-existing", f"{remote_host}:{remote_dir}/pdfs/", P.PDFS + "/"])

    stats = do_merge(canonical, tmp_remote_db, dry_run=dry_run)

    if not dry_run:
        for path in (canonical, tmp_remote_db):
            c2 = sqlite3.connect(path)
            c2.row_factory = sqlite3.Row
            P.reindex_lists(c2)
            c2.commit()
            c2.close()
        rsync([tmp_remote_db, f"{remote_host}:{remote_dir}/library.db"])
    os.remove(tmp_remote_db)

    print(f"local:  +{stats['local_new']} new paper(s), {stats['field_fills_local']} field(s) filled")
    print(f"remote: +{stats['remote_new']} new paper(s), {stats['field_fills_remote']} field(s) filled")
    if stats["conflicts"]:
        print(f"{len(stats['conflicts'])} soft conflict(s) (both sides kept, not auto-resolved):")
        for c in stats["conflicts"][:20]:
            print(f"  - {c}")
        if len(stats["conflicts"]) > 20:
            print(f"  ... and {len(stats['conflicts']) - 20} more")
    if not dry_run and (stats["local_new"] or stats["field_fills_local"]):
        subprocess.run(["python3", os.path.join(os.path.dirname(__file__), "papers.py"), "bib"])
    print("done" + (" (dry-run — nothing written)" if dry_run else ""))


def main():
    ap = argparse.ArgumentParser(description="Sync the papers library (DB + PDFs) with Trillium /scratch.")
    ap.add_argument("--remote-host", default=REMOTE_HOST)
    ap.add_argument("--remote-dir", default=REMOTE_DIR)
    ap.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    ap.add_argument("--no-pdfs", action="store_true", help="sync metadata only, skip the pdfs/ rsync")
    a = ap.parse_args()
    if not (a.remote_host and a.remote_dir):
        sys.exit('no remote configured: add "remote": {"host": "user@host", "path": "/remote/dir"} to '
                 f"{P._tilde(P.PROFILE_PATH)}, or pass --remote-host and --remote-dir")
    run(a.remote_host, a.remote_dir, a.dry_run, sync_pdfs=not a.no_pdfs)


if __name__ == "__main__":
    main()
