#!/usr/bin/env python3
"""
check_sync.py — verify that papers-vela matches Zotero identically.

Compares every current Zotero item against papers-vela and reports:

  1. MISSING  — Zotero items not yet in papers-vela (would be added by sync)
  2. PDF-DRIFT — papers-vela pdf_path doesn't match Zotero's current attachment
  3. DOI-DRIFT — papers-vela has arXiv DOI but Zotero item now carries a journal DOI
  4. ORPHAN    — papers-vela record can't be matched to any live Zotero item
  5. NO-PDF    — papers-vela record has no pdf_path but Zotero has a PDF attached

Safe to run while Zotero is open (immutable WAL read).

Usage:
  python3 check_sync.py [--fix] [--verbose]

  --fix     Auto-repair PDF-DRIFT and DOI-DRIFT in papers-vela (writes library.db).
            MISSING items must be pulled in by running sync.py.
            ORPHAN records are only flagged — never auto-deleted.

Env vars: PAPERS_LIB, PAPERS_VELA, ZOTERO_DB, ZOTERO_DIR (same as sync.py)
"""

import argparse, os, re, sqlite3, sys

PAPERS_VELA = os.path.expanduser(os.environ.get("PAPERS_VELA") or os.environ.get("PAPERS_AGENT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PAPERS_VELA)

import importlib.util as _ilu

def _load(name, path):
    spec = _ilu.spec_from_file_location(name, path)
    mod  = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

_sync = _load("sync", os.path.join(PAPERS_VELA, "zotero", "sync.py"))

LIB        = os.path.expanduser(os.environ.get("PAPERS_LIB") or next((p for p in ("~/Zotero/papers-vela", "~/Zotero/papers-agent")  # the pre-rename name still works
                if os.path.isdir(os.path.expanduser(p))), "~/Zotero/papers-vela"))
DB         = os.path.join(LIB, "library.db")
ZOTERO_DIR = os.path.expanduser(os.environ.get("ZOTERO_DIR", "~/Zotero"))
STORAGE    = os.path.join(ZOTERO_DIR, "storage")


def check(fix=False, verbose=False):
    pa = sqlite3.connect(DB)
    pa.row_factory = sqlite3.Row
    zc, mode = _sync.open_zotero()

    print(f"[check_sync] reading Zotero ({mode} mode)")

    # ── Pull all Zotero items (no since= filter — full scan) ─────────────────
    zotero_items = _sync.query_zotero_items(zc)
    print(f"[check_sync] {len(zotero_items)} Zotero items, "
          f"{sum(1 for i in zotero_items if i['pdf_path'])} with PDFs\n")

    # ── Build papers-vela dedup maps ─────────────────────────────────────────
    by_doi, by_arxiv, by_ck, lib_fps = _sync.build_dedup_maps(pa)

    # ── Build reverse map: pa_id -> full row ──────────────────────────────────
    pa_rows = {r["id"]: r for r in pa.execute(
        "SELECT id, citekey, doi, arxiv, pdf_path, title FROM papers").fetchall()}

    # Track which PA ids were matched by a Zotero item
    matched_pa_ids = set()

    missing   = []  # Zotero item not in PA
    pdf_drift = []  # PA pdf_path != Zotero's current attachment
    doi_drift = []  # PA has arXiv DOI; Zotero now carries journal DOI
    no_pdf    = []  # PA has no pdf_path but Zotero has a PDF

    for item in zotero_items:
        pa_id, reason = _sync.find_existing(item, by_doi, by_arxiv, by_ck, lib_fps)

        if pa_id is None:
            missing.append(item)
            continue

        matched_pa_ids.add(pa_id)
        pa_row = pa_rows[pa_id]

        # ── DOI drift ────────────────────────────────────────────────────────
        z_doi  = item["doi_norm"] or ""
        pa_doi = _sync.norm_doi(pa_row["doi"] or "") or ""
        if (z_doi and "10.48550" not in z_doi and re.match(r"^10\.\d{4}", z_doi)
                and "10.48550" in pa_doi):
            doi_drift.append((pa_id, pa_row, z_doi))

        # ── PDF drift ────────────────────────────────────────────────────────
        z_pdf  = item["pdf_path"]    # Zotero's current attachment path
        pa_pdf = pa_row["pdf_path"]  # what PA thinks the path is

        if z_pdf and pa_pdf and z_pdf != pa_pdf:
            # Only flag if the Zotero PDF actually exists and differs
            if os.path.exists(z_pdf):
                pdf_drift.append((pa_id, pa_row, z_pdf))
        elif z_pdf and not pa_pdf:
            no_pdf.append((pa_id, pa_row, z_pdf))

    # ── Orphans: PA records not matched by any Zotero item ───────────────────
    orphans = [pa_rows[pid] for pid in pa_rows if pid not in matched_pa_ids]

    # ── Report ────────────────────────────────────────────────────────────────
    issues = 0

    # 1. Missing
    if missing:
        print(f"MISSING ({len(missing)}) — in Zotero but not in papers-vela")
        print(f"  → run: python3 zotero/sync.py --once --verbose")
        if verbose:
            for i in missing:
                print(f"  • {(i['title'] or '')[:70]}  doi={i['doi_norm'] or i.get('doi','')}")
        print()
        issues += len(missing)

    # 2. DOI drift
    if doi_drift:
        print(f"DOI-DRIFT ({len(doi_drift)}) — papers-vela has arXiv DOI; "
              f"Zotero item now has journal DOI")
        for pa_id, row, jdoi in doi_drift:
            print(f"  • [{row['citekey']}]")
            print(f"      PA:     {row['doi']}")
            print(f"      Zotero: {jdoi}")
        print()
        issues += len(doi_drift)

    # 3. PDF drift
    if pdf_drift:
        print(f"PDF-DRIFT ({len(pdf_drift)}) — papers-vela pdf_path differs from "
              f"Zotero's current attachment")
        for pa_id, row, z_pdf in pdf_drift:
            print(f"  • [{row['citekey']}]")
            if verbose:
                print(f"      PA:     {row['pdf_path']}")
                print(f"      Zotero: {z_pdf}")
        print()
        issues += len(pdf_drift)

    # 4. No-PDF
    if no_pdf:
        print(f"NO-PDF ({len(no_pdf)}) — Zotero has a PDF but papers-vela pdf_path is NULL")
        for pa_id, row, z_pdf in no_pdf:
            print(f"  • [{row['citekey']}]  →  {os.path.basename(z_pdf)}")
        print()
        issues += len(no_pdf)

    # 5. Orphans
    # Filter out papers that have no DOI/arXiv (can't match) — those are expected misses
    real_orphans = [r for r in orphans
                    if (r["doi"] or r["arxiv"]) and r["doi"] != ""]
    if real_orphans:
        print(f"ORPHAN ({len(real_orphans)}) — papers-vela records with no matching "
              f"live Zotero item")
        print(f"  (Zotero item may have been deleted — review before removing from PA)")
        if verbose:
            for r in real_orphans:
                print(f"  • [{r['citekey']}]  doi={r['doi']}")
        print()
        issues += len(real_orphans)

    # ── Auto-fix ──────────────────────────────────────────────────────────────
    if fix and (doi_drift or pdf_drift or no_pdf):
        print("── Auto-fixing ──────────────────────────────────────────────────")
        fixed = 0

        for pa_id, row, jdoi in doi_drift:
            pa.execute("UPDATE papers SET doi=? WHERE id=?", (jdoi, pa_id))
            print(f"  DOI fixed: [{row['citekey']}] → {jdoi}")
            fixed += 1

        for pa_id, row, z_pdf in pdf_drift:
            pa.execute("UPDATE papers SET pdf_path=?, pdf_on_disk=1 WHERE id=?",
                       (z_pdf, pa_id))
            print(f"  PDF fixed: [{row['citekey']}] → {os.path.basename(z_pdf)}")
            fixed += 1

        for pa_id, row, z_pdf in no_pdf:
            pa.execute("UPDATE papers SET pdf_path=?, pdf_on_disk=1 WHERE id=?",
                       (z_pdf, pa_id))
            print(f"  PDF set:   [{row['citekey']}] → {os.path.basename(z_pdf)}")
            fixed += 1

        pa.commit()
        print(f"\nFixed {fixed} records in papers-vela.")
        issues -= fixed
        print()

    # ── Summary ───────────────────────────────────────────────────────────────
    if issues == 0 and not fix:
        print("✓ papers-vela matches Zotero — no issues found.")
    elif issues == 0:
        print("✓ All fixable issues resolved.")
    else:
        print(f"{'─'*60}")
        total = len(missing) + len(doi_drift) + len(pdf_drift) + len(no_pdf) + len(real_orphans)
        print(f"Summary: {total} issue(s) — "
              f"{len(missing)} missing, {len(doi_drift)} doi-drift, "
              f"{len(pdf_drift)} pdf-drift, {len(no_pdf)} no-pdf, "
              f"{len(real_orphans)} orphan")
        if not fix:
            print("Re-run with --fix to auto-repair doi-drift, pdf-drift, and no-pdf.")

    _sync.close_zotero(zc)
    pa.close()
    return issues


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fix",     action="store_true",
                    help="auto-repair doi-drift, pdf-drift, no-pdf in papers-vela")
    ap.add_argument("--verbose", action="store_true",
                    help="print full details for each issue")
    a = ap.parse_args()

    for path, label in [(DB, "papers-vela library")]:
        if not os.path.exists(path):
            sys.exit(f"{label} not found at {path}")

    issues = check(fix=a.fix, verbose=a.verbose)
    sys.exit(0 if issues == 0 else 1)


if __name__ == "__main__":
    main()
