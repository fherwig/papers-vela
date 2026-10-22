#!/usr/bin/env python3
"""
Generate references.bib + a browsable per-list markdown index from library.db.

- references.bib: one entry per paper (existing citekey where unique, else derived
  LastName_Year; a `file = {pdfs/...}` field points at the PDF for JabRef-style tools).
- lists/<List>.md: per-list tables (year | authors | title | pdf).
- INDEX.md: overview of all lists with counts + PDF coverage.
"""
import os, re, sqlite3, sys, unicodedata
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from papers import require_lib, bibtex_entry, LIB   # library guard, shared entry formatter, resolved library

NEXT = LIB   # resolved like every other command: $PAPERS_LIB, then the profile, then defaults
DB = os.path.join(NEXT, "library.db")
BIB = os.path.join(NEXT, "references.bib")
LISTS_DIR = os.path.join(NEXT, "lists")
INDEX = os.path.join(NEXT, "INDEX.md")


def ascii_fold(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))


def last_name(authors):
    a = (authors or "").strip()
    if not a:
        return "Anon"
    bc = a.split(",")[0]
    if "," in a and " " not in bc.strip():
        ln = bc
    else:
        toks = a.split(" and ")[0].split(",")[0].strip().split()
        ln = toks[-1] if toks else "Anon"
    return re.sub(r"[^A-Za-z]", "", ascii_fold(ln)) or "Anon"


def main():
    require_lib()
    db = sqlite3.connect(DB); db.row_factory = sqlite3.Row
    papers = db.execute(
        "SELECT id,citekey,title,authors,year,journal,volume,issue,pages,doi,arxiv,"
        "abstract,pdf_path,in_cloud,notes FROM papers ORDER BY year, id").fetchall()

    # unique citekeys
    used, key_of = set(), {}
    def uniq(base):
        base = re.sub(r"[^A-Za-z0-9:_-]", "", base) or "ref"
        if base not in used:
            used.add(base); return base
        for c in "abcdefghijklmnopqrstuvwxyz":
            if base + c not in used:
                used.add(base + c); return base + c
        i = 2
        while f"{base}_{i}" in used:
            i += 1
        used.add(f"{base}_{i}"); return f"{base}_{i}"

    for p in papers:
        base = p["citekey"] if p["citekey"] else f"{last_name(p['authors'])}{p['year'] or 'nd'}"
        key_of[p["id"]] = uniq(base)

    # references.bib
    n = 0
    with open(BIB, "w", encoding="utf-8") as f:
        f.write("% Generated from library.db — do not edit by hand.\n\n")
        for p in papers:
            # One entry formatter for references.bib AND the web GUI's BibTeX button
            # (papers.bibtex_entry), so the two cannot drift; it carries the provenance note=.
            f.write(bibtex_entry(p, key_of[p["id"]], with_file=True) + "\n")
            n += 1

    # per-list markdown + INDEX
    os.makedirs(LISTS_DIR, exist_ok=True)
    lists = db.execute("SELECT id,name,source FROM lists ORDER BY source, name").fetchall()
    summ = {r["name"]: r for r in db.execute("SELECT * FROM list_summary")}
    idx = ["# Papers library — list index\n",
           f"_{len(papers)} papers; {sum(1 for p in papers if p['pdf_path'])} with a local PDF; "
           f"{sum(1 for p in papers if p['in_cloud'])} in the current cloud library._\n",
           "_Routing index for the `/papers` skill and the papers-vela chat — match a topic against the themes/domains "
           "below to find the relevant lists. Regenerate with `papers reindex` + `papers bib`._\n"]
    last_src = None
    written = set()                     # pages written now; any other page is for a list that is gone
    for l in lists:
        mem = db.execute(
            "SELECT p.* FROM paper_lists pl JOIN papers p ON p.id=pl.paper_id "
            "WHERE pl.list_id=? ORDER BY p.year DESC, p.id", (l["id"],)).fetchall()
        if not mem:
            continue
        withpdf = sum(1 for m in mem if m["pdf_path"])
        slug = re.sub(r"[^A-Za-z0-9]+", "-", l["name"]).strip("-")
        if l["source"] != last_src:
            idx.append(f"\n## {'Current (cloud) lists' if l['source']=='cloud' else 'Legacy / other lists'}\n")
            last_src = l["source"]
        s = summ.get(l["name"])
        dom = f"  ·  _{s['domains']}_" if s and s["domains"] else ""
        idx.append(f"### [{l['name']}](lists/{slug}.md)  ({len(mem)} papers, {withpdf} pdf){dom}")
        if s and s["blurb"]:
            idx.append(f"{s['blurb']}  ")
        if s and s["themes"]:
            idx.append(f"_themes:_ {s['themes']}\n")
        rows = [f"# {l['name']}  ", f"_{len(mem)} papers, {withpdf} with PDF — source: {l['source']}_\n",
                "| Year | Authors | Title | PDF |", "|---|---|---|---|"]
        for m in mem:
            au = (m["authors"] or "").split(" and ")[0].split(",")[0]
            pdf = f"[pdf](../{m['pdf_path']})" if m["pdf_path"] else ""
            ttl = (m["title"] or "").replace("|", "/")[:90]
            rows.append(f"| {m['year'] or ''} | {au} | {ttl} | {pdf} |")
        open(os.path.join(LISTS_DIR, slug + ".md"), "w", encoding="utf-8").write("\n".join(rows) + "\n")
        written.add(slug + ".md")
    for fn in os.listdir(LISTS_DIR):    # lists/ is generated: nothing in it is anyone's own file
        if fn.endswith(".md") and fn not in written:
            os.remove(os.path.join(LISTS_DIR, fn))
    open(INDEX, "w", encoding="utf-8").write("\n".join(idx) + "\n")

    print(f"references.bib: {n} entries -> {BIB}")
    print(f"per-list pages: {sum(1 for l in lists)} -> {LISTS_DIR}/")
    print(f"index -> {INDEX}")


if __name__ == "__main__":
    main()
