#!/usr/bin/env python3
"""
Backfill missing abstracts + citation counts from NASA ADS for papers that have a
DOI / arXiv id / bibcode-style citekey. Batched ADS queries (identifier:(...)).

Adds a `citation_count` column. Updates abstract only when currently empty; refreshes
citation_count for all matched. Idempotent. Reads token from ~/.config/ads/token.
"""
import os, re, json, sqlite3, sys, time, urllib.parse, urllib.request

LIB = os.path.expanduser("~/Nextcloud/Papers")
DB = os.path.join(LIB, "library.db")
TOKEN = open(os.path.expanduser("~/.config/ads/token")).read().strip()
UA = {"User-Agent": "papers-cli/1.0", "Authorization": f"Bearer {TOKEN}"}
BIBCODE = re.compile(r"\d{4}[A-Za-z0-9.&]{15}")
BATCH = 40


def norm_doi(s):
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", (s or "").strip().lower())


def ads_query(idents):
    q = " OR ".join(f'identifier:"{i}"' for i in idents)
    params = urllib.parse.urlencode({"q": q, "rows": len(idents) * 2,
        "fl": "bibcode,identifier,doi,abstract,citation_count"})
    req = urllib.request.Request("https://api.adsabs.harvard.edu/v1/search/query?" + params, headers=UA)
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.load(r).get("response", {}).get("docs", [])


def main():
    db = sqlite3.connect(DB); db.row_factory = sqlite3.Row
    cols = [r[1] for r in db.execute("PRAGMA table_info(papers)")]
    if "citation_count" not in cols:
        db.execute("ALTER TABLE papers ADD COLUMN citation_count INTEGER")

    rows = db.execute(
        "SELECT id,doi,arxiv,citekey,abstract FROM papers "
        "WHERE (abstract IS NULL OR abstract='' OR citation_count IS NULL)").fetchall()
    # map identifiers -> pid; choose one query identifier per paper
    by_doi, by_bib, by_arx = {}, {}, {}
    work = []
    for r in rows:
        ident = None
        if r["doi"]:
            by_doi[norm_doi(r["doi"])] = r["id"]; ident = r["doi"]
        if r["citekey"] and BIBCODE.fullmatch(r["citekey"]):
            by_bib[r["citekey"]] = r["id"]; ident = ident or r["citekey"]
        if r["arxiv"]:
            by_arx[r["arxiv"].lower()] = r["id"]; ident = ident or r["arxiv"]
        if ident:
            work.append((r["id"], ident, not (r["abstract"] or "").strip()))
    print(f"papers needing backfill with an identifier: {len(work)}")

    def find_pid(doc):
        for d in (doc.get("doi") or []):
            if norm_doi(d) in by_doi: return by_doi[norm_doi(d)]
        if doc.get("bibcode") in by_bib: return by_bib[doc["bibcode"]]
        for idn in (doc.get("identifier") or []):
            m = re.search(r"(\d{4}\.\d{4,5})", idn)
            if m and m.group(1).lower() in by_arx: return by_arx[m.group(1).lower()]
        return None

    abst = cites = 0
    for i in range(0, len(work), BATCH):
        chunk = work[i:i + BATCH]
        try:
            docs = ads_query([w[1] for w in chunk])
        except Exception as e:
            print(f"  batch {i//BATCH} failed: {e}"); continue
        for doc in docs:
            pid = find_pid(doc)
            if not pid: continue
            if doc.get("abstract"):
                cur = db.execute("SELECT abstract FROM papers WHERE id=?", (pid,)).fetchone()
                if not (cur["abstract"] or "").strip():
                    db.execute("UPDATE papers SET abstract=? WHERE id=?", (doc["abstract"], pid)); abst += 1
            if doc.get("citation_count") is not None:
                db.execute("UPDATE papers SET citation_count=? WHERE id=?", (doc["citation_count"], pid)); cites += 1
        db.commit()
        print(f"  ...batch {i//BATCH+1}/{(len(work)+BATCH-1)//BATCH}  (+{abst} abstracts, {cites} cites so far)")
        time.sleep(0.2)

    have = db.execute("SELECT count(*) FROM papers WHERE abstract IS NOT NULL AND abstract!=''").fetchone()[0]
    print(f"\nDONE: filled {abst} abstracts, {cites} citation counts.")
    print(f"  papers with abstract now: {have}")


if __name__ == "__main__":
    main()
