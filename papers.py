#!/usr/bin/env python3
"""
papers — the CLI and engine of papers-vela, a vela for your research library.

First time:  papers init      (writes your profile, ~/.config/papers/profile.json)
Every day:   papers web       (opens the web app; starts it if it is not running)

Commands:
  search <query> [--list L] [--author A] [--year Y] [--limit N]   smart search: "Author 2008",
                                                                  "topic 2007", or ranked full-text
  show <id>                                                       full record + lists
  open <id>                                                       open the PDF (macOS)
  lists                                                           all lists + counts
  list <name>                                                     papers in a list
  add <doi|arxiv|bibcode> [--list L] [--pdf]                      add metadata (PDF on demand; --pdf to fetch now)
  attach <id> <file> [--force]                                   file an existing local PDF onto a paper
  tag <id|doi|arxiv|bibcode> --list L [--list L2] [--remove]      (un)tag an existing paper to list(s)
  refresh <id|doi|arxiv|bibcode> | --missing-year                 re-fetch metadata to fill blanks (e.g. year)
  gaps [--fetch-arxiv]                                            current papers lacking a PDF
  bib                                                             regenerate references.bib + index
  stats                                                           library overview
  clean                                                           normalize title/journal/abstract (strip import artifacts)
  init [--library P] [--email E] [--backend claude|velella]       set up your profile (and a new library)
  profile                                                         show your profile, and your key's fingerprint
  web [stop|status] [--port N]                                    open the web app, starting it if needed
  serve [--port N] [--no-open]                                    run the web server in the foreground
  sync-trillium [--dry-run] [--no-pdfs]                           merge with a remote copy (profile "remote")

Library path: $PAPERS_LIB if set; else "library" in your profile; else ~/.config/papers/lib;
else an existing library.db in ~/Papers or ~/papers-lib. Errors only if none is found.
"""
import argparse, json, os, re, shutil, sqlite3, subprocess, sys, unicodedata, urllib.parse, urllib.request

# ---- THE PROFILE: everything about one user, kept out of the engine ----
# The engine (this repo) is the same for everyone; the library (data) is one folder per user;
# the PROFILE says which library, which model, which email and tokens. Nothing personal is
# hard-coded here any more (2026-10-09, for sharing papers-vela beyond its first user).
#   ~/.config/papers/profile.json   (or $PAPERS_PROFILE)
#   {"library": "~/Papers", "email": "you@uni.edu", "ads_token_file": "~/.config/ads/token",
#    "chat": {"backend": "claude", "model": "claude-sonnet-5-5"}
#         or {"backend": "velella", "key_file": "~/.config/spigot/key"}   (tokens.velella.ca)
#    "remote": {"host": "user@cluster", "path": "/scratch/user/papers-lib"}}    (optional)
# Environment variables still win over the profile, so one-off overrides keep working.
PROFILE_PATH = os.path.expanduser(os.environ.get("PAPERS_PROFILE") or "~/.config/papers/profile.json")


def load_profile():
    try:
        with open(PROFILE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


PROFILE = load_profile()


def profile_get(*keys, default=None):
    d = PROFILE
    for k in keys:
        if not isinstance(d, dict) or k not in d:
            return default
        d = d[k]
    return d if d not in (None, "") else default


# Library location resolution, in priority order:
#   1. $PAPERS_LIB            explicit; used as-is (e.g. to create a new library)
#   2. "library" in the profile
#   3. ~/.config/papers/lib   a file holding one path (the older form of the profile)
#   4. first of DEFAULT_LIB_CANDIDATES that already contains a library.db (auto-discovery)
# Auto-discovery only ever selects an EXISTING library.db, so the tool never creates a
# library in a directory you did not explicitly name. ~/papers-lib is where a cluster copy
# conventionally lives (no profile is needed there).
DEFAULT_LIB_CANDIDATES = ["~/Papers", "~/papers-lib"]
LIB_CONFIG = "~/.config/papers/lib"


def resolve_lib():
    env = os.environ.get("PAPERS_LIB")
    if env:
        return os.path.expanduser(env)
    if profile_get("library"):
        return os.path.expanduser(profile_get("library"))
    cfg = os.path.expanduser(LIB_CONFIG)
    if os.path.isfile(cfg):
        try:
            p = open(cfg, encoding="utf-8").read().strip()
            if p:
                return os.path.expanduser(p)
        except OSError:
            pass
    for cand in DEFAULT_LIB_CANDIDATES:
        d = os.path.expanduser(cand)
        if os.path.isfile(os.path.join(d, "library.db")):
            if sys.stderr.isatty():
                sys.stderr.write(f"papers: using auto-discovered library at {d} "
                                 "(set PAPERS_LIB to override)\n")
            return d
    return ""


LIB = resolve_lib()
DB = os.path.join(LIB, "library.db")
PDFS = os.path.join(LIB, "pdfs")
EMAIL = os.environ.get("PAPERS_EMAIL") or profile_get("email", default="papers-vela@example.com")
UA = {"User-Agent": f"papers-cli/1.0 (mailto:{EMAIL})"}

STOP = {"the","a","an","of","in","on","and","or","for","to","with","from","by","at","as",
        "is","are","be","this","that","their","its","we","our","using","use","via","new","i","ii","iii"}


def require_lib():
    """Exit with a clear message if no library could be located. Resolution order is
    $PAPERS_LIB, then ~/.config/papers/lib, then auto-discovery of an existing library.db
    in the default locations. Auto-discovery never creates a library in a directory you
    didn't name — creating a NEW library still needs an explicit path."""
    if not LIB:
        sys.exit("papers-vela could not locate a library.\n"
                 f"Tried: $PAPERS_LIB, the profile ({_tilde(PROFILE_PATH)}), ~/.config/papers/lib, and defaults "
                 + ", ".join(DEFAULT_LIB_CANDIDATES) + ".\n"
                 "New here? Run:  papers init      (sets up your profile and creates a library)\n"
                 "Or point at an existing library:  export PAPERS_LIB=/path/to/your/library")


def db():
    require_lib()
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c


def fold(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))


def last_name(authors):
    a = (authors or "").strip()
    if not a: return "Anon"
    bc = a.split(",")[0]
    if "," in a and " " not in bc.strip():
        ln = bc
    else:
        t = a.split(" and ")[0].split(",")[0].strip().split(); ln = t[-1] if t else "Anon"
    return re.sub(r"[^A-Za-z]", "", fold(ln)) or "Anon"


def keywords(title, n=3):
    t = re.sub(r"\$[^$]*\$", " ", fold(title or ""))
    t = re.sub(r"\\[A-Za-z]+", " ", t); t = re.sub(r"[{}^_]", " ", t)
    out = []
    for w in re.findall(r"[A-Za-z][A-Za-z-]{2,}", t.lower()):
        w = w.strip("-")
        if len(w) >= 3 and w not in STOP: out.append(w)
        if len(out) >= n: break
    return "-".join(out)


def trunc(s, n): s = s or ""; return s if len(s) <= n else s[:n-1] + "…"


_SYM = {"alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "zeta": "ζ",
        "eta": "η", "theta": "θ", "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν",
        "xi": "ξ", "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ",
        "chi": "χ", "psi": "ψ", "omega": "ω", "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ",
        "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
        "odot": "☉", "sun": "☉", "oplus": "⊕", "pm": "±", "mp": "∓", "times": "×", "sim": "~",
        "approx": "≈", "propto": "∝", "leq": "≤", "geq": "≥", "lesssim": "≲", "gtrsim": "≳"}


def clean_text(s):
    """Render a stored field readably: strip HTML/MathML tags, BibTeX protective braces, and LaTeX
    escapes/math markup (\\&, \\omega, $...$, ...). Unknown LaTeX commands are left as-is rather
    than dropped, so nothing is lost. Display-only — does not change what's in the database."""
    if not s:
        return s
    s = (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
           .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))  # decode entities first
    s = re.sub(r"</?[a-zA-Z!][^<>]*>", "", s)                  # then strip real tags only — a '<'
    #                                                           followed by a letter/'/'  (keeps 'M < 8')
    s = re.sub(r"\\([&%_$#{}])", r"\1", s)                     # \& \% \_ ... -> the bare character
    s = re.sub(r"\\[,;:! ]", " ", s)                           # LaTeX spacing macros (\, \; ...) -> space
    s = re.sub(r"\\([A-Za-z]+)", lambda m: _SYM.get(m.group(1), m.group(0)), s)  # \omega->ω, else keep
    s = s.replace("{", "").replace("}", "").replace("$", "").replace("~", " ")
    return re.sub(r"\s+", " ", s).strip()


def list_tags(c, pid):
    return [r["name"] for r in c.execute(
        "SELECT l.name FROM paper_lists pl JOIN lists l ON l.id=pl.list_id WHERE pl.paper_id=?", (pid,))]


def rowdict(r):
    return {k: r[k] for k in r.keys()} if r is not None else None


def resolve_paper(c, ident):
    """Find a library paper id from an integer id, bibcode (citekey), doi, or arxiv id."""
    ident = (ident or "").strip()
    if re.fullmatch(r"\d+", ident):
        r = c.execute("SELECT id FROM papers WHERE id=?", (int(ident),)).fetchone()
        return r["id"] if r else None
    r = c.execute("SELECT id FROM papers WHERE citekey=?", (ident,)).fetchone()
    if r: return r["id"]
    r = c.execute("SELECT id FROM papers WHERE doi IS NOT NULL AND lower(doi)=?", (ident.lower(),)).fetchone()
    if r: return r["id"]
    aid = re.sub(r"(?i)^arxiv:?", "", ident)
    r = c.execute("SELECT id FROM papers WHERE arxiv=?", (aid,)).fetchone()
    return r["id"] if r else None


def tag_paper(c, pid, names):
    """Add list membership(s) to an existing paper, creating lists as needed.
    Returns the list names that were newly added (already-present ones skipped)."""
    before = set(list_tags(c, pid))
    for lname in names or []:
        l = c.execute("SELECT id FROM lists WHERE name=?", (lname,)).fetchone()
        lid = l["id"] if l else c.execute(
            "INSERT INTO lists(name,source,n_members) VALUES(?,'manual',0)", (lname,)).lastrowid
        c.execute("INSERT OR IGNORE INTO paper_lists(paper_id,list_id) VALUES(?,?)", (pid, lid))
    after = set(list_tags(c, pid))
    return [n for n in (names or []) if n in (after - before)]


def untag_paper(c, pid, names):
    removed = []
    for lname in names or []:
        l = c.execute("SELECT id FROM lists WHERE name=?", (lname,)).fetchone()
        if not l: continue
        if c.execute("DELETE FROM paper_lists WHERE paper_id=? AND list_id=?", (pid, l["id"])).rowcount:
            removed.append(lname)
    return removed


def print_rows(c, rows):
    for p in rows:
        au = last_name(p["authors"])
        flag = "✓" if p["pdf_path"] else " "
        print(f"  [{p['id']:>4}] {flag} {p['year'] or '????'}  {au:<14} {trunc(clean_text(p['title']),62)}")


# ---------------- queries (return data; reused by the CLI and the web server) ----------------
def query_search(c, query="", list=None, author=None, year=None, limit=25):
    """Matching paper Rows (FTS over title/authors/abstract + author/year/list filters).
    Empty `query` => filter-only. Returns [] when an FTS query matches nothing."""
    where, args = [], []
    if query:
        toks = re.findall(r"\w+", query)
        if toks:
            ids = [r["rowid"] for r in c.execute(
                "SELECT rowid FROM papers_fts WHERE papers_fts MATCH ?", (" ".join(toks),))]
            if not ids: return []
            where.append("p.id IN (%s)" % ",".join("?" * len(ids))); args += ids
    if author: where.append("p.authors LIKE ?"); args.append(f"%{author}%")
    if year: where.append("p.year=?"); args.append(year)
    join = ""
    if list:
        join = "JOIN paper_lists pl ON pl.paper_id=p.id JOIN lists l ON l.id=pl.list_id"
        where.append("l.name=?"); args.append(list)
    q = f"SELECT DISTINCT p.* FROM papers p {join}"
    if where: q += " WHERE " + " AND ".join(where)
    q += " ORDER BY p.year DESC, p.id LIMIT ?"; args.append(limit)
    return c.execute(q, args).fetchall()


def query_show(c, pid):
    """(paper Row, [list names]) or None."""
    p = c.execute("SELECT * FROM papers WHERE id=?", (pid,)).fetchone()
    return (p, list_tags(c, p["id"])) if p else None


# ---- BibTeX export: ONE definition for references.bib AND the web GUI's BibTeX button ----
# They used to be two copies, and the GUI's silently lacked the provenance note= that
# references.bib carries. generate_outputs.py now calls bibtex_entry() too.
def bib_value(k, v):
    """Render a BibTeX field value. Titles get double braces ({{...}}) so the whole title's
    capitalization is preserved, plus `_` escaped (subscripts); other fields keep `_` (file paths)."""
    v = str(v).replace("&", r"\&").replace("%", r"\%").replace("#", r"\#")
    return "{{" + v.replace("_", r"\_") + "}}" if k == "title" else "{" + v + "}"


def provenance(p):
    """The '[provenance] ...' line resolve.py writes into notes, or ''."""
    try:
        m = re.search(r"\[provenance\]\s*(.+)", p["notes"] or "", re.S)
    except (KeyError, IndexError):
        return ""
    return " ".join(m.group(1).split()) if m else ""


def bib_fields(p, with_file=False):
    """The fields of one exported entry, in order. `file=` (the local PDF, for JabRef) only in
    references.bib; the note= carries which source validated the record (Falk, 2026-08-14), so a
    manuscript's .bib keeps the evidence with the citation."""
    f = []
    for k, col in (("author", "authors"), ("title", "title"), ("journal", "journal"), ("year", "year"),
                   ("volume", "volume"), ("number", "issue"), ("pages", "pages"), ("doi", "doi"),
                   ("eprint", "arxiv")):
        if p[col]:
            f.append((k, p[col]))
    if with_file and p["pdf_path"]:
        f.append(("file", p["pdf_path"]))
    prov = provenance(p)
    if prov:
        f.append(("note", prov))
    return f


def bib_key(p):
    """The \\cite key a record exports under (references.bib adds a/b/... only on a collision)."""
    return re.sub(r"[^A-Za-z0-9:_-]", "", p["citekey"] or f"{last_name(p['authors'])}{p['year'] or 'nd'}") or "ref"


def bibtex_entry(p, key=None, with_file=False):
    """A single @article entry for a paper Row, exactly as references.bib writes it."""
    key = key or bib_key(p)
    body = ",\n  ".join(f"{k} = {bib_value(k, v)}" for k, v in bib_fields(p, with_file))
    return f"@article{{{key},\n  {body}\n}}\n"


def query_verification(c, rows):
    """{paper id: verdict} for paper Rows, combining BOTH records of the citation campaign:
    the verification table (the Crossref-tier check gate.py/audit write) and the provenance
    note resolve.py writes after walking the hierarchy journal > ADS > arXiv. A record any tier
    validated IS validated, even when the Crossref check disagrees — reading the table alone
    flags dozens of good records (the authority is often the wrong side). verdict:
      state   verified | override | unverified | unchecked
      label   short human text;  detail  the finding / override reason, for a tooltip"""
    ids = [r["id"] for r in rows]
    vt = {}
    if ids:
        try:
            for v in c.execute("SELECT * FROM verification WHERE paper_id IN (%s)"
                               % ",".join("?" * len(ids)), ids):
                vt[v["paper_id"]] = v
        except sqlite3.OperationalError:             # library built before the gate existed
            pass
    out = {}
    for r in rows:
        v, prov = vt.get(r["id"]), provenance(r)
        via = re.match(r"Verified against (\w+)", prov)
        status = v["status"] if v else None
        if status == "override":
            out[r["id"]] = {"state": "override", "label": "admitted by override",
                            "detail": v["override_reason"] or ""}
        elif status in ("verified", "soft") or via:
            src = via.group(1) if via else (v["authority"] or "")
            note = "" if status in ("verified", "soft", None) else f"{v['authority'] or 'crossref'} check: {status}"
            out[r["id"]] = {"state": "verified", "label": f"verified ({src})" if src else "verified",
                            "detail": "; ".join(x for x in (note, v["detail"] if v else "", prov) if x)}
        elif v:
            out[r["id"]] = {"state": "unverified", "label": f"not verified ({status})",
                            "detail": v["detail"] or ""}
        else:
            out[r["id"]] = {"state": "unchecked", "label": "never checked", "detail": ""}
    return out


def query_paper_text(c, pid, max_chars=16000):
    """Metadata plus body text for one paper, for the chat to read. Prefers PDF text extracted
    with pdftotext (sandboxed to the library's pdfs/ dir); falls back to the stored abstract."""
    p = c.execute("SELECT * FROM papers WHERE id=?", (pid,)).fetchone()
    if not p:
        return None
    head = (f"[{p['id']}] {clean_text(p['title'])} — {clean_text(p['authors'])} "
            f"({p['year'] or '?'}, {clean_text(p['journal']) or '?'})")
    body, src, exe = "", "abstract", shutil.which("pdftotext")
    if exe and p["pdf_path"]:
        real = os.path.realpath(os.path.join(LIB, p["pdf_path"]))
        if real.startswith(os.path.realpath(PDFS) + os.sep) and os.path.isfile(real):
            try:
                txt = subprocess.run([exe, "-q", "-l", "15", real, "-"],
                                     capture_output=True, text=True, timeout=20).stdout
                body = re.sub(r"\n{3,}", "\n\n", txt or "").strip()[:max_chars]
                if body:
                    src = f"extracted PDF text, ~{len(body)} chars"
            except Exception:
                body = ""
    if not body:
        body = (clean_text(p["abstract"]) or "(no abstract and no extractable PDF text available)").strip()
    return f"{head}\n[source: {src}]\n{body}"


def query_lists(c):
    return c.execute(
        "SELECT l.name, l.source, count(pl.paper_id) n, "
        "sum(CASE WHEN p.pdf_path IS NOT NULL THEN 1 ELSE 0 END) wp "
        "FROM lists l LEFT JOIN paper_lists pl ON pl.list_id=l.id LEFT JOIN papers p ON p.id=pl.paper_id "
        "GROUP BY l.id ORDER BY n DESC").fetchall()


def query_list(c, name):
    return c.execute(
        "SELECT p.* FROM papers p JOIN paper_lists pl ON pl.paper_id=p.id JOIN lists l ON l.id=pl.list_id "
        "WHERE l.name=? ORDER BY p.year DESC, p.id", (name,)).fetchall()


def query_index(c, domain=None, grep=None, refresh=True):
    """Routing view: one dict per list (blurb/themes/domains/counts/span/anchors) plus a
    computed `status` of ok|stale|no-blurb. Refresh recomputes the cheap auto layer first."""
    ensure_summary_table(c)
    if refresh:
        reindex_lists(c)
    rows = c.execute(
        "SELECT l.name AS name, l.source AS source, s.blurb, s.themes, s.domains, s.n_blurb, "
        "s.n_members, s.n_pdf, s.ymin, s.ymax, s.top_keywords, s.anchors "
        "FROM lists l LEFT JOIN list_summary s ON s.name=l.name").fetchall()
    out = []
    for r in rows:
        if r["n_members"] is None:
            continue
        if domain and domain.lower() not in (r["domains"] or "").lower():
            continue
        if grep:
            hay = " ".join(str(r[k] or "") for k in ("name", "blurb", "themes", "domains", "top_keywords")).lower()
            if grep.lower() not in hay:
                continue
        d = rowdict(r)
        d["status"] = "ok" if (r["blurb"] and r["n_blurb"] == r["n_members"]) else ("stale" if r["blurb"] else "no-blurb")
        out.append(d)
    out.sort(key=lambda d: -(d["n_members"] or 0))
    return out


# ---------------- smart free-text search (shared by CLI, web box, and chat retrieval) ----------
SEARCH_STOP = STOP | {"paper", "papers", "about", "which", "what", "where", "who", "find", "show",
                      "tell", "give", "cover", "covers", "list", "lists", "name", "have", "has",
                      "any", "some", "one", "specific", "id", "ids", "do", "does",
                      "summarize", "summarise", "explain", "describe"}
AUTHOR_YEAR = re.compile(r"^\s*(.+?)\s+((?:18|19|20)\d{2})\s*(?:[-–to]+\s*((?:18|19|20)\d{2}))?\s*$")


def search_terms(text):
    """Salient lowercase terms from free text (filler/stopwords dropped, deduped, capped)."""
    seen, out = set(), []
    for t in re.findall(r"[A-Za-z][A-Za-z-]{2,}", (text or "").lower()):
        if t in SEARCH_STOP or t in seen:
            continue
        seen.add(t); out.append(t)
    return out[:12]


def fts_ranked(c, text, limit=25, year=None, year2=None, prefix=False):
    """Relevance-ranked full-text search: salient terms OR-ed, then ranked by exact-phrase match
    (title > anywhere) > first-author match > column-weighted bm25 (title & authors boosted) >
    recency. Optional year (or year range) filter. Returns [] when the text has no usable terms.
    prefix=True uses term* matching so partial words resolve (e.g. 'Herw' finds 'Herwig')."""
    if prefix:
        # No stopword filter and no minimum length — live search should match whatever the
        # user typed, including "the" or "s". Split on hyphens to avoid FTS5 treating '-'
        # as NOT (e.g. 's-process*' → OperationalError without the split).
        seen, terms = set(), []
        for t in re.findall(r"[A-Za-z][A-Za-z-]*", (text or "").lower()):
            for part in t.split("-"):
                if part and part not in seen:
                    seen.add(part); terms.append(part)
        terms = terms[:12]
    else:
        terms = search_terms(text)
    if not terms:
        return []
    match_expr = " OR ".join(f'{t}*' if prefix else f'"{t}"' for t in terms)
    where = ["papers_fts MATCH ?"]
    year_args = []
    if year and year2:
        where.append("p.year BETWEEN ? AND ?"); year_args += [year, year2]
    elif year:
        where.append("p.year=?"); year_args.append(year)

    # Ranking tiers (highest weight first), then column-weighted bm25, then recency:
    #   1. exact phrase in the title        4. weighted bm25 (title & authors boosted)
    #   2. exact phrase anywhere            5. year DESC (recency tiebreaker)
    #   3. query matches the FIRST author
    # ponytail: phrase match normalizes only hyphens SQL-side ('s-process' == 's process');
    # a phrase spanning other punctuation (':' etc.) won't match — rare for real queries.
    like = "%" + re.sub(r"\s+", " ", (text or "").lower().replace("-", " ")).strip() + "%"
    first_author = "substr(p.authors,1,instr(p.authors||' and ',' and ')-1)"
    sql = (
        "SELECT p.*, bm25(papers_fts, 8.0, 4.0, 1.0, 0.5) AS rank, "
        "  (replace(lower(p.title),'-',' ') LIKE ?) AS ph_title, "
        "  (replace(lower(p.title||' '||COALESCE(p.abstract,'')),'-',' ') LIKE ?) AS ph_any, "
        f"  (replace(lower({first_author}),'-',' ') LIKE ?) AS fa_match "
        "FROM papers p JOIN papers_fts ON papers_fts.rowid=p.id "
        "WHERE " + " AND ".join(where) + " "
        "ORDER BY ph_title DESC, ph_any DESC, fa_match DESC, rank, p.year DESC LIMIT ?"
    )
    args = [like, like, like, match_expr, *year_args, limit]
    try:
        return c.execute(sql, args).fetchall()
    except sqlite3.OperationalError:
        return []


def papers_by_author(c, name, limit, year=None, year2=None):
    """Papers where `name` is an author SURNAME — matched as a whole token (the text before the
    comma in each "Last, First" entry), NOT a substring, so 'Issa' ignores 'Krissansen-Totton'
    and 'Pelissard'. Ordered first-author papers first, then co-authored, each newest-first.
    Optional single year or [year, year2] range filter. Returns [] when no paper has that
    surname, so the caller can fall through to a topic search."""
    name = name.lower()
    where, args = ["lower(authors) LIKE ?"], [f"%{name}%"]   # cheap SQL pre-filter
    if year and year2:
        where.append("year BETWEEN ? AND ?"); args += [year, year2]
    elif year:
        where.append("year=?"); args.append(year)
    hits = []
    for r in c.execute("SELECT * FROM papers WHERE " + " AND ".join(where), args):
        surs = [a.split(",")[0].strip().lower() for a in (r["authors"] or "").split(" and ")]
        if not surs:
            continue
        if surs[0] == name:                              # first author
            hits.append((0, r))
        elif name in surs:                               # co-author
            hits.append((1, r))
    hits.sort(key=lambda mr: (mr[0], -(mr[1]["year"] or 0), -mr[1]["id"]))
    return [r for _, r in hits[:limit]]


def smart_search(c, text, limit=25, prefix=False):
    """Free-text query understanding, no LLM:
      • '<author> <year>'  (optional range)        -> author + year lookup;
      • '<topic> <year>'   (when no such author)    -> topic terms + that year/range;
      • '<topic>'                                   -> ranked full-text.
    The author branch falls through to the topic branch, so 'OC shell mergers 2007' filters
    shell-merger papers to 2007 once it's clear there's no author by that name.
    prefix=True enables partial-word FTS matching (for live/as-you-type search)."""
    text = (text or "").strip()
    if not text:
        return []
    m = AUTHOR_YEAR.match(text)
    if m:
        lead, y1 = m.group(1).strip(), int(m.group(2))
        y2 = int(m.group(3)) if m.group(3) else None
        rows = papers_by_author(c, lead, limit, y1, y2)
        if rows:
            return rows                              # the leading text was an author name
        topic = fts_ranked(c, lead, limit, y1, y2, prefix=prefix)
        if topic:
            return topic
    # Bare author surname (a single token that actually names an author in the library):
    # an author lookup, not a topic search.
    if re.fullmatch(r"[A-Za-z][A-Za-z'’-]{2,}", text):
        rows = papers_by_author(c, text, limit)
        if rows:
            return rows
    return fts_ranked(c, text, limit, prefix=prefix)


# ---------------- commands ----------------
def cmd_search(a):
    c = db()
    if a.query and not (a.list or a.author or a.year):
        rows = smart_search(c, a.query, a.limit)     # 'author year', 'topic year', or topical
    else:
        rows = query_search(c, a.query, a.list, a.author, a.year, a.limit)
    if not rows and a.query:
        print("no matches."); return
    print(f"{len(rows)} result(s):")
    print_rows(c, rows)


def cmd_show(a):
    c = db(); res = query_show(c, a.id)
    if not res: print("not found"); return
    p, _lists = res
    print(f"[{p['id']}] {clean_text(p['title'])}")
    print(f"  authors : {clean_text(p['authors'])}")
    print(f"  year    : {p['year']}   journal: {clean_text(p['journal'])}   vol {p['volume']} {p['pages'] or ''}")
    print(f"  doi     : {p['doi']}    arxiv: {p['arxiv']}")
    print(f"  lists   : {', '.join(_lists) or '—'}")
    print(f"  pdf     : {p['pdf_path'] or '— (no local PDF)'}")
    if p["abstract"]: print(f"\n  {trunc(clean_text(p['abstract']), 600)}")
    cm = query_comments(c, p["id"])
    if cm:
        print(f"\n  comments ({len(cm)}):")
        for k in cm:
            where = f"p.{k['page']}" if k["page"] else "paper"
            print(f"    #{k['id']} {where}: " + (f"“{trunc(k['quote'], 90)}”" if k["quote"] else "")
                  + (f"  — {k['note']}" if k["note"] else ""))


def cmd_open(a):
    c = db(); p = c.execute("SELECT pdf_path FROM papers WHERE id=?", (a.id,)).fetchone()
    if not p or not p["pdf_path"]: print("no PDF for that id"); return
    path = os.path.join(LIB, p["pdf_path"])
    if not os.path.exists(path): print(f"file missing: {path}"); return
    subprocess.run(["open", path])


def cmd_lists(a):
    c = db()
    for r in query_lists(c):
        print(f"  {r['n']:>4} ({r['wp'] or 0:>4} pdf)  {r['name']}  [{r['source'] or '-'}]")


def cmd_list(a):
    c = db()
    rows = query_list(c, a.name)
    if not rows: print(f"no list named '{a.name}' (try: papers lists)"); return
    print(f"{a.name}: {len(rows)} papers")
    print_rows(c, rows)


def http_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


# Authors are stored the way BibTeX and the rest of the library expect them: "Last, First and
# Last, First". Both fetchers below used to write "First Last, First Last", which BibTeX reads as
# ONE garbled author, so every DOI- or arXiv-added paper exported a wrong author list (2026-10-06).
_PARTICLES = {"van", "von", "der", "den", "de", "del", "della", "di", "da", "du", "le", "la",
              "ter", "ten", "dos", "das", "y"}
_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}


def last_first(name):
    """'Paul R. Woodward' -> 'Woodward, Paul R.'; 'Pieter van der Berg' -> 'van der Berg, Pieter';
    'Icko Iben Jr.' -> 'Iben, Jr., Icko'. A name already containing a comma is returned as is."""
    name = " ".join((name or "").split())
    if not name or "," in name:
        return name
    t = name.split()
    suffix = t.pop() if len(t) > 2 and t[-1].lower() in _SUFFIXES else ""
    if len(t) == 1:
        return t[0]
    i = next((k for k in range(1, len(t) - 1) if t[k].lower() in _PARTICLES), len(t) - 1)
    return ", ".join(x for x in (" ".join(t[i:]), suffix, " ".join(t[:i])) if x)


def norm_doi(s):
    """Strip common DOI prefixes (doi:, https://doi.org/, dx.doi.org) and lowercase."""
    if not s: return s
    s = re.sub(r"(?i)^\s*(doi:\s*|https?://(dx\.)?doi\.org/)", "", s.strip())
    return s.lower()


def fetch_crossref(doi):
    doi = norm_doi(doi)
    m = http_json(f"https://api.crossref.org/works/{urllib.parse.quote(doi)}")["message"]
    au = " and ".join(
        (f"{x['family']}, {x['given']}" if x.get("given") else x["family"]) if x.get("family")
        else (x.get("name") or "") for x in m.get("author", []))
    yr = (m.get("issued", {}).get("date-parts", [[None]])[0] or [None])[0]
    return {"title": clean_text((m.get("title") or [""])[0]), "authors": au, "year": yr,
            "journal": clean_text((m.get("container-title") or [""])[0]), "volume": m.get("volume"),
            "issue": m.get("issue"), "pages": m.get("page"), "doi": doi.lower(),
            "arxiv": None, "abstract": clean_text(m.get("abstract", "") or "")}


def fetch_arxiv(aid):
    req = urllib.request.Request(f"http://export.arxiv.org/api/query?id_list={aid}", headers=UA)
    xml = urllib.request.urlopen(req, timeout=30).read().decode()
    # Scope to the <entry> block. The Atom feed carries a feed-level <title> ("ArXiv Query:
    # search_query=&id_list=...") ahead of the paper's own, so an unscoped search returns the
    # query string as the title and the admission gate then refuses every arXiv-only add.
    m_entry = re.search(r"<entry>(.*?)</entry>", xml, re.S)
    entry = m_entry.group(1) if m_entry else xml
    def g(tag): m = re.search(rf"<{tag}>(.*?)</{tag}>", entry, re.S); return (m.group(1).strip() if m else "")
    authors = re.findall(r"<author>\s*<name>(.*?)</name>", entry, re.S)
    doi = g("arxiv:doi")
    return {"title": clean_text(g("title")), "authors": " and ".join(last_first(clean_text(a)) for a in authors),
            "year": (g("published")[:4] or None), "journal": "arXiv", "volume": None,
            "issue": None, "pages": None, "doi": doi.lower() or None, "arxiv": aid,
            "abstract": clean_text(g("summary"))}


def download(url, dest):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        f.write(r.read())
    return os.path.getsize(dest) > 1000


def unpaywall_pdf(doi):
    """A direct OA PDF URL for a DOI, preferring the publisher-hosted version of record over a
    repository (e.g. arXiv) copy."""
    try:
        d = http_json(f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi)}?email={EMAIL}")
        locs = [l for l in (d.get("oa_locations") or []) if l.get("url_for_pdf")]
        pub = next((l for l in locs if l.get("host_type") == "publisher"), None)
        loc = pub or (d.get("best_oa_location") or {})
        return loc.get("url_for_pdf")
    except Exception:
        return None


def is_pdf(path):
    try:
        with open(path, "rb") as f:
            return f.read(5).startswith(b"%PDF")
    except OSError:
        return False


def download_pdf(arxiv, doi, dest):
    """Download the preferred open-access PDF: the journal/publisher version (Unpaywall — the
    version of record) first, then arXiv. arXiv may be a preprint, so the journal copy wins when
    available. Validates the result is actually a PDF (some OA links return an HTML landing page)."""
    urls = []
    if doi:
        u = unpaywall_pdf(doi)
        if u: urls.append(u)
    if arxiv:
        urls.append(f"https://arxiv.org/pdf/{arxiv}.pdf")
    for u in urls:
        try:
            if download(u, dest) and is_pdf(dest):
                return True
        except Exception:
            continue
    return False


def unique_name(c, stem):
    base, ext = stem, ".pdf"
    existing = {os.path.basename(r["pdf_path"]) for r in
                c.execute("SELECT pdf_path FROM papers WHERE pdf_path IS NOT NULL")}
    if base + ext not in existing: return base + ext
    for ch in "abcdefghijklmnopqrstuvwxyz":
        if base + ch + ext not in existing: return base + ch + ext
    return base + "_x" + ext


def fetch_pdf(c, pid):
    """Download an open-access PDF (arXiv, else Unpaywall) for an existing paper that lacks one,
    file it under a homogenized name, update pdf_path, and refresh the routing layer.
    Returns (ok: bool, message). Only pulls from arXiv / Unpaywall — never an arbitrary URL."""
    p = c.execute("SELECT * FROM papers WHERE id=?", (pid,)).fetchone()
    if not p:
        return False, "no such paper"
    if p["pdf_path"]:
        return True, p["pdf_path"]
    if not (p["arxiv"] or p["doi"]):
        return False, "no open-access source (no arXiv id or DOI)"
    stem = f"{last_name(p['authors'])}_{p['year'] or 'nd'}"
    kw = keywords(p["title"])
    name = unique_name(c, (stem + (f"_{kw}" if kw else ""))[:90])
    dest = os.path.join(PDFS, name)
    try:
        if not download_pdf(p["arxiv"], p["doi"], dest):
            return False, "no working open-access PDF found (tried the journal copy, then arXiv)"
    except Exception as e:
        return False, f"download failed: {e}"
    c.execute("UPDATE papers SET pdf_path=?,pdf_on_disk=1 WHERE id=?", (f"pdfs/{name}", pid))
    c.commit()
    reindex_lists(c)
    return True, f"pdfs/{name}"


# ---------------- NASA ADS ----------------
ADS_TOKEN_FILE = os.path.expanduser(profile_get("ads_token_file", default="~/.config/ads/token"))


def ads_token():
    try:
        return open(ADS_TOKEN_FILE).read().strip() or None
    except OSError:
        return None


def ads_get(path, params):
    tok = ads_token()
    if not tok:
        sys.exit(f"No NASA ADS token at {ADS_TOKEN_FILE}. Get one (free) at "
                 "https://ui.adsabs.harvard.edu/user/settings/token and run `papers init` to store it.")
    url = "https://api.adsabs.harvard.edu/v1/" + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={**UA, "Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def ads_search(query, rows=20, sort="date desc", extra_fields=""):
    fl = "bibcode,title,author,year,citation_count,doi,identifier,pub" + (f",{extra_fields}" if extra_fields else "")
    d = ads_get("search/query", {"q": query, "rows": rows, "sort": sort, "fl": fl})
    return d.get("response", {}).get("docs", [])


def ads_arxiv_id(doc):
    """The arXiv id among an ADS doc's identifiers, or None."""
    for ident in doc.get("identifier", []) or []:
        m = re.search(r"(?i)arxiv:?\s*(\d{4}\.\d{4,5})", ident) or re.fullmatch(r"\d{4}\.\d{4,5}", ident)
        if m:
            return m.group(1) if m.groups() else m.group(0)
    return None


def normalize_ident(s):
    """Reduce what people paste to the bare identifier `add` dispatches on: arXiv abs/pdf URLs and
    versioned ids (2610.01239v2) -> 2610.01239; doi.org URLs and 'doi:' -> the DOI; ADS abstract
    URLs -> the bibcode. Anything else is returned stripped and unchanged."""
    s = urllib.parse.unquote((s or "").strip())
    m = re.search(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", s, re.I) or \
        re.fullmatch(r"(?i)(?:arxiv:)?\s*(\d{4}\.\d{4,5})(?:v\d+)?", s)
    if m:
        return m.group(1)
    m = re.search(r"adsabs\.harvard\.edu/abs/([^/?#\s]{19})", s, re.I)
    if m:
        return m.group(1)
    m = re.match(r"(?i)(?:https?://(?:dx\.)?doi\.org/|doi:\s*)(10\.\S+)", s)
    return m.group(1) if m else s


def fetch_ads_bibcode(bibcode):
    docs = ads_get("search/query", {"q": f"bibcode:{bibcode}", "rows": 1,
                   "fl": "bibcode,title,author,year,doi,identifier,pub,volume,page,abstract,issue"}
                   ).get("response", {}).get("docs", [])
    if not docs:
        sys.exit("bibcode not found in ADS")
    d = docs[0]
    arxiv = ads_arxiv_id(d)
    doi = (d.get("doi") or [None])[0]
    return {"title": clean_text((d.get("title") or [""])[0]), "authors": " and ".join(d.get("author", [])),
            "year": d.get("year"), "journal": clean_text(d.get("pub")), "volume": d.get("volume"),
            "issue": d.get("issue"), "pages": (d.get("page") or [None])[0],
            "doi": (doi.lower() if doi else None), "arxiv": arxiv,
            "abstract": clean_text(d.get("abstract", "")), "bibcode": bibcode}


def cmd_ads(a):
    docs = ads_search(a.query, a.rows, "citation_count desc" if a.cited else "date desc")
    print(f"{len(docs)} ADS hits for: {a.query}")
    for d in docs:
        au = (d.get("author") or ["?"])[0].split(",")[0]
        cc = d.get("citation_count", 0) or 0
        print(f"  {d['bibcode']}  {d.get('year','????')}  c{cc:<5} {au:<14} {trunc((d.get('title') or ['?'])[0], 50)}")
    print('  → import:  papers add <bibcode> --list "<list>"')


def cmd_add(a):
    ident = normalize_ident(a.ident)          # accepts arXiv/doi.org/ADS URLs and versioned ids
    is_bibcode = bool(re.fullmatch(r"\d{4}[A-Za-z0-9.&]{15}", ident))
    is_arxiv = (not is_bibcode) and (bool(re.fullmatch(r"\d{4}\.\d{4,5}", ident)) or ident.lower().startswith("arxiv"))
    aid = re.sub(r"(?i)^arxiv:?", "", ident) if is_arxiv else None
    if is_bibcode:
        print(f"fetching ADS bibcode {ident} ...")
        meta = fetch_ads_bibcode(ident); aid = meta.get("arxiv")
    elif is_arxiv:
        print(f"fetching arXiv {aid} ...")
        meta = fetch_arxiv(aid)
    else:
        print(f"fetching DOI {ident} ...")
        meta = fetch_crossref(ident)
    if not meta.get("title"): sys.exit("could not fetch metadata")

    # ---- ADMISSION GATE (gate.py) -------------------------------------------------------
    # Even though the metadata above came FROM an authority, it is checked against the
    # identifier's own authority before it can enter. That is not redundant: an ADS bibcode can
    # carry a DOI whose Crossref record is a different paper, and a wrong DOI RESOLVES -- it just
    # resolves elsewhere (2026-08-14). Refusal is the default; the override is recorded.
    import gate
    ok, vstatus, vauth, vident, vratio, vdetail = gate.validate(meta)
    if not ok and not a.force_unverified:
        sys.exit(gate.refusal_message(vstatus, vident, vdetail,
                                      ctx="while adding: " + trunc(meta["title"], 70)))
    if not ok:
        print(f"  ⚠ admitted UNVERIFIED ({vstatus}) by explicit override — recorded in the "
              f"verification table and listed by `papers audit --overrides`")

    c = db()
    dup = None
    if meta.get("bibcode"):
        dup = c.execute("SELECT id FROM papers WHERE citekey=?", (meta["bibcode"],)).fetchone()
    if not dup and meta.get("doi"):
        dup = c.execute("SELECT id FROM papers WHERE lower(doi)=?", (meta["doi"],)).fetchone()
    if not dup and meta.get("arxiv"):        # a fresh preprint has no DOI: without this, adding
        dup = c.execute("SELECT id FROM papers WHERE arxiv=?", (meta["arxiv"],)).fetchone()  # it twice made two rows
    if dup:
        if a.list:
            added = tag_paper(c, dup["id"], a.list)
            c.commit(); reindex_lists(c, a.list)
            print(f"already in library as id {dup['id']}; tagged +{', '.join(added) or '(already present)'}"
                  f"  →  lists: {', '.join(list_tags(c, dup['id']))}")
        else:
            print(f"already in library as id {dup['id']} (use: papers show {dup['id']})")
        return
    stem = f"{last_name(meta['authors'])}_{meta['year'] or 'nd'}"
    kw = keywords(meta["title"]);  stem += f"_{kw}" if kw else ""
    name = unique_name(c, stem[:90])
    pdf_rel = None
    if a.pdf and not a.no_pdf:                # PDFs are fetched on demand by default; --pdf opts in
        dest = os.path.join(PDFS, name)
        try:
            if download_pdf(meta.get("arxiv"), meta.get("doi"), dest):
                pdf_rel = f"pdfs/{name}"; print(f"  downloaded PDF -> {pdf_rel}")
            else:
                print("  no open-access PDF found; adding metadata only")
        except Exception as e:
            print(f"  PDF download failed ({e}); adding metadata only")
    cur = c.execute(
        "INSERT INTO papers(citekey,title,authors,year,journal,volume,issue,pages,doi,arxiv,"
        "abstract,is_duplicate,source,in_cloud,pdf_on_disk,pdf_path) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,0,'added',1,?,?)",
        (meta.get("bibcode") or (stem.split('_')[0] + str(meta['year'] or '')), meta["title"], meta["authors"], meta["year"],
         meta["journal"], meta["volume"], meta["issue"], meta["pages"], meta["doi"], meta["arxiv"],
         meta["abstract"], 1 if pdf_rel else 0, pdf_rel))
    pid = cur.lastrowid
    gate.record(c, pid, "override" if not ok else vstatus, vauth, vident, vratio, vdetail,
                override_reason=(a.force_unverified if not ok else None))
    for lname in (a.list or []):
        l = c.execute("SELECT id FROM lists WHERE name=?", (lname,)).fetchone()
        lid = l["id"] if l else c.execute(
            "INSERT INTO lists(name,source,n_members) VALUES(?,'manual',0)", (lname,)).lastrowid
        c.execute("INSERT OR IGNORE INTO paper_lists(paper_id,list_id) VALUES(?,?)", (pid, lid))
    c.commit()
    if a.list:
        reindex_lists(c, a.list)     # self-refresh the routing layer for affected lists
    print(f"added id {pid}: {trunc(meta['title'],60)}  lists={a.list or '—'}")
    if not pdf_rel and (meta.get("arxiv") or meta.get("doi")):
        print("  (metadata only — fetch the PDF on demand in the web GUI, with `papers gaps --fetch-arxiv`, or re-add with --pdf)")
    print("  (auto layer refreshed; run `papers bib` to also regenerate references.bib + INDEX.md)")


def cmd_gaps(a):
    c = db()
    rows = c.execute("SELECT * FROM papers WHERE in_cloud=1 AND pdf_path IS NULL ORDER BY year DESC").fetchall()
    print(f"{len(rows)} current-library papers without a PDF "
          f"({sum(1 for r in rows if r['arxiv'])} have arXiv)")
    if not a.fetch_arxiv:
        print_rows(c, rows[:40]);
        if len(rows) > 40: print(f"  ... +{len(rows)-40} more")
        print("\n(use --fetch-arxiv to pull the arXiv-available ones)")
        return
    got = 0
    for p in rows:
        if not p["arxiv"]: continue
        stem = f"{last_name(p['authors'])}_{p['year'] or 'nd'}"; kw = keywords(p["title"])
        name = unique_name(c, (stem + (f"_{kw}" if kw else ""))[:90])
        dest = os.path.join(PDFS, name)
        try:
            if download(f"https://arxiv.org/pdf/{p['arxiv']}.pdf", dest):
                c.execute("UPDATE papers SET pdf_path=?,pdf_on_disk=1 WHERE id=?", (f"pdfs/{name}", p["id"]))
                got += 1; print(f"  ✓ {p['arxiv']}  {trunc(p['title'],50)}")
        except Exception as e:
            print(f"  ✗ {p['arxiv']} ({e})")
    c.commit()
    if got:
        reindex_lists(c)             # self-refresh after changing PDF coverage
    print(f"fetched {got} PDFs from arXiv; auto layer refreshed. (run `papers bib` to regenerate references.bib + INDEX.md)")


def cmd_bib(a):
    subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "generate_outputs.py")])


def cmd_stats(a):
    c = db(); g = lambda q: c.execute(q).fetchone()[0]
    nlists = g("SELECT count(*) FROM lists")
    ncloud = g("SELECT count(*) FROM lists WHERE source='cloud'")
    print(f"library: {LIB}")
    print(f"  papers           : {g('SELECT count(*) FROM papers')}")
    print(f"  in current cloud : {g('SELECT count(*) FROM papers WHERE in_cloud=1')}")
    print(f"  with local PDF   : {g('SELECT count(*) FROM papers WHERE pdf_path IS NOT NULL')}")
    print(f"  lists            : {nlists} (cloud {ncloud}, other {nlists - ncloud})")


# ---------------- list index (routing/awareness layer) ----------------
def ensure_summary_table(c):
    c.execute("""CREATE TABLE IF NOT EXISTS list_summary(
      name TEXT PRIMARY KEY, blurb TEXT, themes TEXT, domains TEXT, n_blurb INTEGER,
      n_members INTEGER, n_pdf INTEGER, ymin INTEGER, ymax INTEGER,
      top_keywords TEXT, top_authors TEXT, anchors TEXT, updated_at TEXT)""")


# ---- comments: the reader's marks on a paper, saved WITH the paper (web GUI's PDF viewer) ----
# In library.db, not in the PDF: PDFs are never edited in place (the Trillium rsync and the
# Zotero backend both rely on that), and a comment belongs to the paper record, not to a file.
# Each carries the QUOTE (the selected text) as well as the rects, so the comment is readable
# without a PDF parser (CLI `show`, the chat); the rects only redraw the highlight. Rects are
# page space at scale 1 — CSS px / render scale, top-left origin — so they hold at any zoom.
# Defined here AND in import_bib.py's SCHEMA (same pattern as list_summary).
COMMENTS_SQL = """CREATE TABLE IF NOT EXISTS comments(
  id INTEGER PRIMARY KEY, paper_id INTEGER NOT NULL,
  page INTEGER,          -- 1-based; NULL = a comment on the paper as a whole
  quote TEXT,            -- the marked text (a region mark: the text inside the box)
  note TEXT,             -- the reader's comment
  rects TEXT,            -- JSON [[x0,y0,x1,y1],...] page space at scale 1
  kind TEXT,             -- text | region | note
  created_at TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS ix_comments_paper ON comments(paper_id);"""


def ensure_comments_table(c):
    c.executescript(COMMENTS_SQL)


def query_comments(c, pid):
    """[{id, page, quote, note, rects, kind, created_at, updated_at}] for one paper, in page order."""
    try:
        rows = c.execute("SELECT * FROM comments WHERE paper_id=? ORDER BY COALESCE(page, 0), id",
                         (pid,)).fetchall()
    except sqlite3.OperationalError:                 # library from before comments existed
        return []
    out = []
    for r in rows:
        d = rowdict(r)
        try:
            d["rects"] = json.loads(r["rects"] or "[]")
        except ValueError:
            d["rects"] = []
        out.append(d)
    return out


def add_comment(c, pid, page=None, quote="", note="", rects=None, kind="text"):
    """Store one comment on paper `pid`; returns its id. Rects are validated to numbers."""
    import datetime
    if not c.execute("SELECT 1 FROM papers WHERE id=?", (pid,)).fetchone():
        raise ValueError("no such paper")
    clean = [[round(float(v), 2) for v in r[:4]] for r in (rects or []) if isinstance(r, list) and len(r) >= 4]
    if not (clean or (note or "").strip()):
        raise ValueError("a comment needs a marked place or a note")
    ensure_comments_table(c)
    now = datetime.datetime.now().isoformat(timespec="seconds")
    cur = c.execute("INSERT INTO comments(paper_id,page,quote,note,rects,kind,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (pid, int(page) if page else None, (quote or "").strip()[:4000],
                     (note or "").strip()[:8000], json.dumps(clean),
                     kind if kind in ("text", "region", "note") else "text", now, now))
    c.commit()
    return cur.lastrowid


def update_comment(c, pid, cid, note):
    import datetime
    n = c.execute("UPDATE comments SET note=?, updated_at=? WHERE id=? AND paper_id=?",
                  ((note or "").strip()[:8000], datetime.datetime.now().isoformat(timespec="seconds"),
                   cid, pid)).rowcount
    c.commit()
    return n


def delete_comment(c, pid, cid):
    n = c.execute("DELETE FROM comments WHERE id=? AND paper_id=?", (cid, pid)).rowcount
    c.commit()
    return n


def reindex_lists(c, names=None):
    """Recompute the auto layer (counts/span/keywords/authors/anchors) for the given
    lists (all if None). Cheap; called automatically by index/add/gaps so the routing
    layer is never stale without anyone remembering to run it."""
    import collections, datetime
    ensure_summary_table(c)
    if names is None:
        names = [r["name"] for r in c.execute("SELECT name FROM lists")]
    today = datetime.date.today().isoformat()
    done = 0
    for name in names:
        rows = c.execute(
            "SELECT p.title,p.authors,p.year,p.tags,p.pdf_path,p.citation_count FROM papers p "
            "JOIN paper_lists pl ON pl.paper_id=p.id JOIN lists l ON l.id=pl.list_id WHERE l.name=?",
            (name,)).fetchall()
        if not rows:
            continue
        ys = [r["year"] for r in rows if r["year"]]
        kw = collections.Counter()
        generic = STOP | {"stars", "stellar", "astrophysics", "star"}
        for r in rows:
            for w in re.findall(r"[A-Za-z][A-Za-z-]{3,}", fold(r["title"] or "").lower()):
                w = w.strip("-")
                if len(w) >= 4 and w not in generic:
                    kw[w] += 1
        au = collections.Counter(last_name(r["authors"]) for r in rows)
        cited = sorted(rows, key=lambda r: (r["citation_count"] if r["citation_count"] is not None else -1), reverse=True)[:3]
        anchors = " ⦙ ".join(
            f"{last_name(r['authors'])} {r['year'] or '?'}: {trunc(r['title'], 45)} (c{r['citation_count'] if r['citation_count'] is not None else '?'})"
            for r in cited)
        c.execute(
            "INSERT INTO list_summary(name,n_members,n_pdf,ymin,ymax,top_keywords,top_authors,anchors,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
            "n_members=excluded.n_members,n_pdf=excluded.n_pdf,ymin=excluded.ymin,ymax=excluded.ymax,"
            "top_keywords=excluded.top_keywords,top_authors=excluded.top_authors,anchors=excluded.anchors,"
            "updated_at=excluded.updated_at",
            (name, len(rows), sum(1 for r in rows if r["pdf_path"]),
             min(ys) if ys else None, max(ys) if ys else None,
             ", ".join(w for w, _ in kw.most_common(8)),
             ", ".join(f"{a}({n})" for a, n in au.most_common(5)), anchors, today))
        done += 1
    c.commit()
    return done


def cmd_reindex(a):
    n = reindex_lists(db(), [a.list] if a.list else None)
    print(f"reindexed {n} lists (auto layer: counts, span, keywords, authors, citation anchors)")


def cmd_index(a):
    c = db()
    out = query_index(c, a.domain, a.grep, refresh=not getattr(a, "no_refresh", False))
    for r in out:
        flag = "" if r["status"] == "ok" else (" ⚠stale" if r["status"] == "stale" else " ⚠no-blurb")
        dom = f"  [{r['domains']}]" if r["domains"] else ""
        print(f"● {r['name']}{dom}  ({r['n_members']}p/{r['n_pdf']}pdf, {r['ymin'] or '?'}–{r['ymax'] or '?'}){flag}")
        if r["blurb"]:
            print(f"    {r['blurb']}")
        if r["themes"]:
            print(f"    themes: {r['themes']}")
        elif r["top_keywords"]:
            print(f"    keywords: {r['top_keywords']}")
        if a.anchors and r["anchors"]:
            print(f"    key: {r['anchors']}")
    print(f"\n{len(out)} lists.  filters: --domain D  --grep TERM  (--anchors to show top-cited)")


def cmd_setsummary(a):
    c = db(); ensure_summary_table(c)
    row = c.execute("SELECT n_members FROM list_summary WHERE name=?", (a.name,)).fetchone()
    nb = row["n_members"] if row else None
    c.execute(
        "INSERT INTO list_summary(name,blurb,themes,domains,n_blurb) VALUES(?,?,?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET blurb=excluded.blurb,themes=excluded.themes,"
        "domains=excluded.domains,n_blurb=excluded.n_blurb",
        (a.name, a.blurb, a.themes, a.domains, nb))
    c.commit(); print(f"summary set for {a.name}")


def cmd_loadsum(a):
    c = db(); ensure_summary_table(c)
    data = json.load(open(a.file))
    for d in data:
        row = c.execute("SELECT n_members FROM list_summary WHERE name=?", (d["name"],)).fetchone()
        nb = row["n_members"] if row else None
        c.execute(
            "INSERT INTO list_summary(name,blurb,themes,domains,n_blurb) VALUES(?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET blurb=excluded.blurb,themes=excluded.themes,"
            "domains=excluded.domains,n_blurb=excluded.n_blurb",
            (d["name"], d.get("blurb"), d.get("themes"), d.get("domains"), nb))
    c.commit(); print(f"loaded {len(data)} summaries")


def cmd_clean(a):
    """One-time normalization: rewrite title/journal/abstract in place, stripping import artifacts
    (BibTeX braces, LaTeX escapes, HTML/MathML). Authors are left alone so BibTeX corporate-author
    braces survive in references.bib. The UPDATE triggers keep the FTS index in sync."""
    c = db()
    n = 0
    for r in c.execute("SELECT id, title, journal, abstract FROM papers").fetchall():
        upd = {}
        for f in ("title", "journal", "abstract"):
            cleaned = clean_text(r[f])
            if cleaned != r[f]:
                upd[f] = cleaned
        if upd:
            c.execute("UPDATE papers SET " + ",".join(f"{k}=?" for k in upd) + " WHERE id=?",
                      (*upd.values(), r["id"]))
            n += 1
    c.commit()
    print(f"cleaned {n} record(s) — stripped BibTeX/LaTeX/HTML artifacts from title/journal/abstract.")
    print("  back up library.db first if you haven't; run `papers bib` to refresh references.bib + INDEX.md.")


def cmd_attach(a):
    """File an existing local PDF onto a paper record: copy it into the library's pdfs/ under the
    homogenized `LastName_Year_keywords.pdf` name and set pdf_path. Use when you already have the
    PDF and don't want to (re-)download it (`add` only fetches PDFs). For a plain-directory library;
    a Zotero-backed library manages its own PDFs via the zotero/ scripts."""
    import shutil
    c = db()
    p = c.execute("SELECT id, authors, year, title, pdf_path FROM papers WHERE id=?", (a.id,)).fetchone()
    if not p:
        sys.exit(f"no paper with id {a.id} (try: papers show {a.id})")
    src = os.path.expanduser(a.file)
    if not os.path.isfile(src):
        sys.exit(f"file not found: {src}")
    with open(src, "rb") as f:
        if f.read(4) != b"%PDF":
            sys.exit(f"{src} does not look like a PDF (no %PDF header)")
    if p["pdf_path"] and not a.force:
        sys.exit(f"[{a.id}] already has a PDF ({p['pdf_path']}); pass --force to replace it")
    stem = f"{last_name(p['authors'])}_{p['year'] or 'nd'}"
    kw = keywords(p["title"])
    name = unique_name(c, (stem + (f"_{kw}" if kw else ""))[:90])
    os.makedirs(PDFS, exist_ok=True)
    shutil.copy2(src, os.path.join(PDFS, name))
    c.execute("UPDATE papers SET pdf_path=?, pdf_on_disk=1 WHERE id=?", (f"pdfs/{name}", a.id))
    c.commit()
    reindex_lists(c)
    print(f"attached {src}\n  -> pdfs/{name}  (id {a.id}: {trunc(clean_text(p['title']), 55)})")
    print("  run `papers bib` to add the file= link to references.bib")


def cmd_serve(a):
    """Launch the localhost web GUI (browse + read-only chat). Hands off to web/server.py."""
    web = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "server.py")
    if not os.path.exists(web):
        sys.exit(f"web server not found at {web}")
    argv = [sys.executable, web, "--port", str(a.port)]
    if a.no_open: argv.append("--no-open")
    os.execv(sys.executable, argv)


# ---------------- setup: profile, key, web launcher ----------------
VELELLA_URL = "https://tokens.velella.ca/v1"
WEB_CACHE = os.path.expanduser("~/.cache/papers-web")


def _tilde(p):
    """Write paths into the profile with ~, so the file reads the same on any machine."""
    p, home = os.path.expanduser(p), os.path.expanduser("~")
    return "~" + p[len(home):] if p == home or p.startswith(home + os.sep) else p


def _ask(question, default, assume_default):
    if assume_default or not sys.stdin.isatty():
        return default
    v = input(f"{question}\n  [{default}] > " if default else f"{question}\n  > ").strip()
    return v or default


def key_fingerprint(path):
    """sha256 of the key exactly as it is sent (the file's content, stripped). This is what the
    token spigot stores and what you send to its operator; the key itself never leaves here."""
    import hashlib
    try:
        return hashlib.sha256(open(os.path.expanduser(path)).read().strip().encode()).hexdigest()
    except OSError:
        return None


def _write_private(path, text):
    path = os.path.expanduser(path)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)


def cmd_init(a):
    """Set up papers-vela for one user: library folder, contact email, NASA ADS token, chat model.
    Writes ~/.config/papers/profile.json. Safe to re-run: current answers are the defaults."""
    import getpass, secrets
    prof = dict(PROFILE)
    chat = dict(prof.get("chat") or {})
    print(f"papers-vela setup  (profile: {_tilde(PROFILE_PATH)})\n")

    # 1. the library: a folder of DATA (library.db + pdfs/), never code
    lib = os.path.expanduser(a.library or _ask("1. Where should your library live? (a folder for library.db and pdfs/)",
                                              _tilde(prof.get("library") or LIB or "~/Papers"), a.yes))
    db_path = os.path.join(lib, "library.db")
    created = not os.path.isfile(db_path)
    os.makedirs(os.path.join(lib, "pdfs"), exist_ok=True)
    c = sqlite3.connect(db_path)
    if created:
        import import_bib
        c.executescript(import_bib.SCHEMA)
    ensure_comments_table(c)
    c.commit(); c.close()
    prof["library"] = _tilde(lib)
    print(f"   {'created a new, empty' if created else 'using the existing'} library at {_tilde(lib)}\n")

    # 2. contact email for Crossref / Unpaywall (their "polite pool"); optional
    email = a.email if a.email is not None else _ask(
        "2. Your email, sent to Crossref and Unpaywall as a courtesy contact (optional)", prof.get("email", ""), a.yes)
    if email:
        prof["email"] = email
    print()

    # 3. NASA ADS token: searching ADS and adding by bibcode need one (free)
    tok = os.path.expanduser(prof.get("ads_token_file") or "~/.config/ads/token")
    prof["ads_token_file"] = _tilde(tok)
    if os.path.isfile(tok):
        print(f"3. NASA ADS token: found ({_tilde(tok)})\n")
    else:
        print("3. NASA ADS token: not set. Get one (free) at https://ui.adsabs.harvard.edu/user/settings/token")
        t = "" if (a.yes or not sys.stdin.isatty()) else getpass.getpass("   paste it here (input hidden; Enter to skip): ").strip()
        if t:
            _write_private(tok, t + "\n"); print(f"   saved to {_tilde(tok)} (only you can read it)\n")
        else:
            print("   skipped; run `papers init` again when you have it\n")

    # 4. the chat's model
    backend = a.backend or _ask(
        "4. Which model should the chat use?\n"
        "     claude   your own Claude subscription, through Claude Code (install it and log in once)\n"
        "     velella  tokens.velella.ca, an open-weight model hosted for the group (needs enrolment)",
        chat.get("backend", "claude"), a.yes)
    if backend not in ("claude", "velella"):
        sys.exit(f"unknown chat backend {backend!r} (use claude or velella)")
    if backend == "claude":
        chat = {"backend": "claude", "model": chat.get("model") if chat.get("backend") == "claude" and chat.get("model")
                else "claude-sonnet-5-5"}
        cl = shutil.which("claude") or next((p for p in (os.path.expanduser("~/.local/bin/claude"),
                                                         os.path.expanduser("~/.claude/local/claude")) if os.path.exists(p)), None)
        print(f"   Claude Code: {'found at ' + cl if cl else 'NOT FOUND — install it (https://claude.com/claude-code) and run `claude` once to log in'}\n")
    else:
        kf = chat.get("key_file") if chat.get("backend") == "velella" and chat.get("key_file") else "~/.config/spigot/key"
        chat = {"backend": "velella", "url": chat.get("url") or VELELLA_URL, "key_file": kf}
        if not os.path.isfile(os.path.expanduser(kf)):
            _write_private(kf, "vsk-" + secrets.token_hex(32) + "\n")
            print(f"   made you a new key in {kf} (only you can read it; never share it)")
        print(f"   Your key's FINGERPRINT (safe to share):\n\n     {key_fingerprint(kf)}\n\n"
              "   Send the fingerprint, not the key, to the operator of tokens.velella.ca and ask to be\n"
              "   enrolled. The chat works once they confirm. A lost key cannot be recovered, only\n"
              "   replaced: delete the file, run `papers init` again, and send the new fingerprint.\n")
    prof["chat"] = chat

    _write_private(PROFILE_PATH, json.dumps(prof, indent=2) + "\n")
    print(f"Saved {_tilde(PROFILE_PATH)}.\n\nNext:\n  papers web            open the web app"
          f"\n  papers add <doi|arXiv id|ADS bibcode>   or   python3 import_bib.py your.bib   to fill the library")


def cmd_profile(a):
    """Show the profile and what it resolves to. Never prints a key."""
    print(f"profile : {_tilde(PROFILE_PATH)}" + ("" if PROFILE else "   (none yet — run `papers init`)"))
    n = None
    if LIB and os.path.isfile(DB):
        try: n = sqlite3.connect(DB).execute("SELECT count(*) FROM papers").fetchone()[0]
        except sqlite3.Error: pass
    print(f"library : {_tilde(LIB) if LIB else '— not found'}" + (f"   ({n} papers)" if n is not None else ""))
    print(f"email   : {EMAIL}")
    print(f"ADS     : {'token found' if os.path.isfile(ADS_TOKEN_FILE) else 'no token'} ({_tilde(ADS_TOKEN_FILE)})")
    chat = profile_get("chat", default={}) or {}
    b = os.environ.get("PAPERS_CHAT_BACKEND") or chat.get("backend", "claude")
    if b == "velella":
        kf = chat.get("key_file", "~/.config/spigot/key")
        fp = key_fingerprint(kf)
        print(f"chat    : tokens.velella.ca ({chat.get('url', VELELLA_URL)})")
        print(f"key     : {kf}" + (f"\nfingerprint (send this, never the key): {fp}" if fp else "   — MISSING, run `papers init`"))
    else:
        print(f"chat    : Claude via Claude Code, model {os.environ.get('PAPERS_WEB_MODEL') or chat.get('model', 'claude-sonnet-5-5')}")
    rem = profile_get("remote")
    if rem:
        print(f"remote  : {rem.get('host')}:{rem.get('path')}   (papers sync-trillium)")


def cmd_web(a):
    """Open the web app; start the server first if it is not already running. `stop` stops it."""
    import socket, signal, time, webbrowser
    port = a.port
    urlf, pidf = os.path.join(WEB_CACHE, f"url-{port}"), os.path.join(WEB_CACHE, f"pid-{port}")

    def up():
        with socket.socket() as so:
            so.settimeout(0.3)
            return so.connect_ex(("127.0.0.1", port)) == 0

    def url():
        try: return open(urlf).read().strip()
        except OSError: return f"http://127.0.0.1:{port}/"

    if a.action == "status":
        print(f"running on port {port}: {url()}" if up() else f"not running (port {port})"); return
    if a.action == "stop":
        pids = []
        try: pids = [int(open(pidf).read().strip())]
        except (OSError, ValueError): pass
        if not pids and shutil.which("lsof"):
            out = subprocess.run(["lsof", "-nP", "-ti", f"tcp:{port}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout
            pids = [int(x) for x in out.split()]
        for pid in pids:
            try: os.kill(pid, signal.SIGTERM)
            except ProcessLookupError: pass
        print(f"stopped (pid {', '.join(map(str, pids))})" if pids else "not running"); return
    if up():
        print(f"already running — opening {url()}")
        webbrowser.open(url()); return
    require_lib()
    if not os.path.isfile(DB):
        sys.exit(f"no library at {DB} — run `papers init`")
    os.makedirs(WEB_CACHE, exist_ok=True)
    log = os.path.join(WEB_CACHE, f"serve-{port}.log")
    t0 = time.time()
    with open(log, "a") as lf:
        subprocess.Popen([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "server.py"),
                          "--port", str(port), "--no-open"], stdout=lf, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True, env={**os.environ, "PAPERS_LIB": LIB})
    for _ in range(60):                                  # wait for a FRESH url file (new token)
        if up() and os.path.isfile(urlf) and os.path.getmtime(urlf) >= t0 - 1:
            print(f"papers-vela is up — {url()}\n  (log: {_tilde(log)}; stop with `papers web stop`)")
            webbrowser.open(url()); return
        time.sleep(0.25)
    sys.exit(f"the server did not come up within 15 s — see {_tilde(log)}")


def backup_db(tag):
    """Copy library.db aside before any bulk write. The library is live data; bulk changes are
    reviewable diffs against a backup, never in-place edits with no way back."""
    import shutil, glob, datetime
    dest = f"{DB}.bak-{tag}-{datetime.datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(DB, dest)
    old = sorted(glob.glob(f"{DB}.bak-{tag}-*"))[:-3]      # keep the most recent 3
    for p in old:
        try: os.remove(p)
        except OSError: pass
    return dest


def cmd_audit(a):
    """Re-run verification and FAIL LOUDLY on any regression.

    A library verified once and never again decays silently: DOIs get corrected upstream,
    records get hand-edited, and a new entry can slip in through a path the gate does not
    cover. This is the recurring half of the guarantee -- the gate stops bad entries at the
    door, this proves the ones already inside still check out.

    Exit codes:  0 = clean   1 = REGRESSION (something that used to verify no longer does)
                 2 = unverified records exist and --strict was given
    """
    import concurrent.futures
    import gate, verify as V

    c = db()
    gate.ensure_table(c)
    rows = c.execute("SELECT id,citekey,source,title,authors,year,journal,volume,pages,doi,arxiv "
                     "FROM papers ORDER BY id").fetchall()
    prior = {r["paper_id"]: dict(r) for r in
             c.execute("SELECT * FROM verification").fetchall()}

    if a.overrides:
        ov = [p for p in prior.values() if p["status"] == "override"]
        print(f"{len(ov)} recorded override(s) — every one is a declared exception, not a bypass:\n")
        for p in sorted(ov, key=lambda x: x["checked_at"] or ""):
            pr = c.execute("SELECT citekey,title FROM papers WHERE id=?", (p["paper_id"],)).fetchone()
            print(f"  [{p['paper_id']}] {(pr['citekey'] if pr else '?')[:28]:28s} {p['checked_at']}")
            print(f"       reason: {p['override_reason']}")
            print(f"       title : {trunc(clean_text(pr['title']) if pr else '?', 72)}")
        return 0

    targets = [dict(r) for r in rows
               if V.norm_doi(r["doi"]) or V.norm_arxiv(r["arxiv"])]
    no_ident = [dict(r) for r in rows
                if not (V.norm_doi(r["doi"]) or V.norm_arxiv(r["arxiv"]))]
    if a.limit:
        targets = targets[:a.limit]

    print(f"papers audit — checking {len(targets)} record(s) with identifiers "
          f"against Crossref/arXiv ({len(no_ident)} have none) ...", flush=True)

    current = {}
    # arXiv mints DOIs through DataCite, so Crossref 404s on every 10.48550/arxiv.* -- routing
    # them to Crossref reported 200+ perfectly good records as "does not resolve", i.e. a weekly
    # ALERT on a false regression, which is how a guard gets ignored. Same rule as
    # verify.load_targets(); keep the two in step.
    for p in targets:
        _ad = V.doi_is_arxiv(V.norm_doi(p["doi"]))
        if _ad:
            p["arxiv"] = p["arxiv"] or _ad
            p["doi"] = ""
    dois = [p for p in targets if V.norm_doi(p["doi"])]
    arxs = [p for p in targets if not V.norm_doi(p["doi"])]

    with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(V.fetch_crossref, V.norm_doi(p["doi"])): p for p in dois}
        for f in concurrent.futures.as_completed(futs):
            p = futs[f]
            try: status, meta = f.result()
            except Exception as e: status, meta = "error:%s" % type(e).__name__, None
            current[p["id"]] = _grade(p, "crossref", V.norm_doi(p["doi"]), status, meta, V)

    by_aid = {}
    for p in arxs:
        by_aid.setdefault(V.norm_arxiv(p["arxiv"]), []).append(p)
    aids = list(by_aid)
    for k in range(0, len(aids), 50):
        chunk = aids[k:k + 50]
        got = V.fetch_arxiv_batch(chunk)
        for i in chunk:
            status, meta = got.get(i, ("error:missing", None))
            for p in by_aid[i]:
                current[p["id"]] = _grade(p, "arxiv", i, status, meta, V)

    GOOD = ("verified", "soft")
    regressions, still_bad, fixed, newly_checked = [], [], [], []
    for pid, cur in current.items():
        was = prior.get(pid)
        if was and was["status"] == "override":
            continue                                  # declared exception; not a regression
        prev_good = bool(was) and was["status"] in GOOD
        now_good = cur["status"] in GOOD
        if prev_good and not now_good:
            regressions.append((pid, was["status"], cur))
        elif not now_good:
            (still_bad if was else newly_checked).append((pid, cur))
        elif was and not prev_good:
            fixed.append((pid, cur))

    ungated = [p for p in targets if p["id"] not in prior]

    if not a.dry_run:
        backup_db("audit")
        for pid, cur in current.items():
            was = prior.get(pid)
            if was and was["status"] == "override":
                continue                              # never overwrite a declared exception
            gate.record(c, pid, cur["status"], cur["authority"], cur["ident"],
                        cur["ratio"], cur["detail"])
        c.commit()

    n_bad = len(regressions) + len(still_bad) + len(newly_checked)
    print()
    print("=" * 72)
    if regressions:
        print(f"!! REGRESSION: {len(regressions)} record(s) that previously verified NO LONGER DO")
        print("=" * 72)
        for pid, oldst, cur in regressions[:40]:
            pr = c.execute("SELECT citekey FROM papers WHERE id=?", (pid,)).fetchone()
            print(f"  [{pid}] {(pr['citekey'] if pr else '?')[:30]:30s} {oldst} -> {cur['status']}")
            print(f"        {cur['detail'][:150]}")
    else:
        print("no regressions: everything that verified before still verifies")
        print("=" * 72)
    print(f"\n  checked      {len(current)}")
    print(f"  verified/soft{sum(1 for x in current.values() if x['status'] in GOOD):>6}")
    print(f"  mismatch     {sum(1 for x in current.values() if x['status']=='mismatch'):>6}")
    print(f"  unresolvable {sum(1 for x in current.values() if x['status'] in ('unresolvable','unreachable')):>6}")
    print(f"  overrides    {sum(1 for p in prior.values() if p['status']=='override'):>6}")
    print(f"  no identifier{len(no_ident):>6}  (no DOI/arXiv id; resolve.py checks these against NASA ADS)")
    if fixed:
        print(f"  newly fixed  {len(fixed):>6}")
    if ungated:
        print(f"\n  note: {len(ungated)} record(s) had no prior verification row — they entered "
              f"before the gate existed (or around it). Now recorded.")

    if regressions:
        print("\nEXIT 1 — a regression is a citation that may now be wrong in every manuscript "
              "that cites it.")
        return 1
    if a.strict and n_bad:
        print(f"\nEXIT 2 — --strict and {n_bad} record(s) do not verify.")
        return 2
    return 0


def _grade(p, authority, ident, status, meta, V):
    if status == "notfound":
        return {"status": "unresolvable", "authority": authority, "ident": ident,
                "ratio": 0.0, "detail": f"{ident} does not resolve at {authority}"}
    if status != "ok" or not meta:
        return {"status": "unreachable", "authority": authority, "ident": ident,
                "ratio": 0.0, "detail": f"could not reach {authority} ({status})"}
    verdict, hard, soft, ratio = V.compare(p, meta)
    return {"status": ("mismatch" if hard else ("soft" if soft else "verified")),
            "authority": authority, "ident": ident, "ratio": ratio,
            "detail": "; ".join(hard + soft)}


def cmd_checkrefs(a):
    """Verify references in a document somebody ELSE wrote. Read-only; never touches the library.

    Hands off to check_refs.py. Deliberately exempt from require_lib(): a referee checking a
    manuscript needs no library at all, and not opening one is the structural guarantee that
    another group's bibliography can never become our records by side effect."""
    import check_refs
    return check_refs.run(a.file, a.as_json, a.strict, a.offline, a.workers)


def cmd_synctrillium(a):
    """Bidirectional sync of library.db + pdfs/ with Trillium /scratch. Hands off to
    trillium_sync.py — merges by content identity (doi/arxiv/citekey), never by the
    local `id` column, so two independently-writable copies merge safely. Laptop-only
    (Trillium has no path back here)."""
    sync = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trillium_sync.py")
    if not os.path.exists(sync):
        sys.exit(f"trillium_sync.py not found at {sync}")
    argv = [sys.executable, sync]
    if a.dry_run: argv.append("--dry-run")
    if a.no_pdfs: argv.append("--no-pdfs")
    os.execv(sys.executable, argv)


def cmd_tag(a):
    c = db()
    pid = resolve_paper(c, a.ident)
    if pid is None:
        sys.exit(f"no paper matching '{a.ident}' (give a library id, bibcode, doi, or arxiv; "
                 f"use `papers add` to fetch a new paper)")
    if not a.list:
        sys.exit("specify at least one --list")
    if a.remove:
        ch = untag_paper(c, pid, a.list); verb = "removed from"
    else:
        ch = tag_paper(c, pid, a.list); verb = "tagged +"
    c.commit(); reindex_lists(c, a.list)
    print(f"[{pid}] {verb} {', '.join(ch) or '(no change)'}  →  lists: {', '.join(list_tags(c, pid)) or '—'}")
    print("  (auto layer refreshed; run `papers bib` to regenerate references.bib + INDEX.md)")


def cmd_renamelist(a):
    """Rename a list in place. paper_lists references list_id, not name, so this only
    touches lists.name (and the keyed-by-name list_summary auto-layer row) — no per-paper
    re-tagging needed."""
    c = db()
    old = c.execute("SELECT id FROM lists WHERE name=?", (a.old,)).fetchone()
    if not old:
        sys.exit(f"no list named '{a.old}' (try: papers lists)")
    if c.execute("SELECT 1 FROM lists WHERE name=?", (a.new,)).fetchone():
        sys.exit(f"a list named '{a.new}' already exists — merging lists isn't supported by this command")
    n = c.execute("SELECT COUNT(*) n FROM paper_lists WHERE list_id=?", (old["id"],)).fetchone()["n"]
    c.execute("UPDATE lists SET name=? WHERE id=?", (a.new, old["id"]))
    c.execute("UPDATE list_summary SET name=? WHERE name=?", (a.new, a.old))
    c.commit(); reindex_lists(c, [a.new])
    print(f"renamed '{a.old}' -> '{a.new}' ({n} paper(s))")
    print("  run `papers bib` to regenerate references.bib + INDEX.md + by-list/")


def cmd_deletelist(a):
    """Delete a list. Refuses if it still has members unless --force (which also drops
    those memberships from paper_lists; the papers themselves are never touched)."""
    c = db()
    row = c.execute("SELECT id FROM lists WHERE name=?", (a.name,)).fetchone()
    if not row:
        sys.exit(f"no list named '{a.name}' (try: papers lists)")
    n = c.execute("SELECT COUNT(*) n FROM paper_lists WHERE list_id=?", (row["id"],)).fetchone()["n"]
    if n and not a.force:
        sys.exit(f"'{a.name}' still has {n} paper(s) — retag them elsewhere first, or pass --force to delete anyway")
    if n:
        c.execute("DELETE FROM paper_lists WHERE list_id=?", (row["id"],))
    c.execute("DELETE FROM lists WHERE id=?", (row["id"],))
    c.execute("DELETE FROM list_summary WHERE name=?", (a.name,))
    c.commit()
    print(f"deleted list '{a.name}'" + (f" (removed {n} membership(s))" if n else " (was empty)"))


def fetch_by_ident(ident):
    """Fetch metadata for an explicit bibcode / arxiv / doi (same dispatch as `add`)."""
    ident = normalize_ident(ident)
    is_bibcode = bool(re.fullmatch(r"\d{4}[A-Za-z0-9.&]{15}", ident))
    is_arxiv = (not is_bibcode) and (bool(re.fullmatch(r"\d{4}\.\d{4,5}", ident)) or ident.lower().startswith("arxiv"))
    if is_bibcode: return fetch_ads_bibcode(ident)
    if is_arxiv: return fetch_arxiv(re.sub(r"(?i)^arxiv:?", "", ident))
    return fetch_crossref(ident)


def refetch_meta(p):
    """Re-fetch metadata for an existing paper from its best available source."""
    ck = p["citekey"]
    if ck and re.fullmatch(r"\d{4}[A-Za-z0-9.&]{15}", ck):
        try: return fetch_ads_bibcode(ck)
        except (Exception, SystemExit): pass
    if p["doi"]:
        try: return fetch_crossref(p["doi"])
        except (Exception, SystemExit): pass
    if p["arxiv"]:
        try: return fetch_arxiv(p["arxiv"])
        except (Exception, SystemExit): pass
    return None


def cmd_refresh(a):
    c = db()
    if a.missing_year:
        rows = c.execute("SELECT * FROM papers WHERE year IS NULL").fetchall()
    elif a.ident:
        pid = resolve_paper(c, a.ident)
        if pid is None: sys.exit(f"no paper matching '{a.ident}'")
        rows = c.execute("SELECT * FROM papers WHERE id=?", (pid,)).fetchall()
    else:
        sys.exit("give a library id/bibcode/doi/arxiv, or --missing-year")
    fields = ("year", "journal", "volume", "issue", "pages", "doi", "arxiv", "title", "authors", "abstract")
    fixed = 0
    for p in rows:
        meta = fetch_by_ident(a.from_) if (getattr(a, "from_", None) and not a.missing_year) else refetch_meta(p)
        if not meta:
            print(f"  [{p['id']}] no refetch source (no bibcode/doi/arxiv) — skipped"); continue
        if getattr(a, "overwrite", False):
            # Correct fields that are present but WRONG. refresh otherwise only fills blanks, so a
            # record admitted with a bad value from one authority could never be corrected against
            # another -- which is how three publication years sat wrong in this library while every
            # gate reported the record verified. Requires an explicit --from, so the correction is
            # always attributed to a named authority record rather than to whoever ran the command.
            only = set(a.fields.split(",")) if getattr(a, "fields", None) else None
            # Compare as strings. The authorities return year as '2024' and the column holds the
            # int 2024, so a raw != is always true for year: the flag would rewrite it on every
            # run, re-stamp the verification row each time, and report a change when there was
            # none. A tool that always says it changed something cannot be used to see whether it
            # did.
            def differs(f):
                return str(meta[f]).strip() != ("" if p[f] is None else str(p[f]).strip())
            upd = {f: meta[f] for f in fields
                   if meta.get(f) and differs(f) and (only is None or f in only)}
        else:
            upd = {f: meta[f] for f in fields if meta.get(f) and not p[f]}
        if upd:
            c.execute("UPDATE papers SET " + ", ".join(f"{k}=?" for k in upd) + " WHERE id=?",
                      (*upd.values(), p["id"]))
            fixed += 1
            print(f"  [{p['id']}] +{', '.join(upd)}  (year={upd.get('year', p['year'])})")
            if getattr(a, "overwrite", False):
                # The verification row says which authority a record was last checked against and
                # when. An overwrite that leaves it alone makes the library assert it was verified
                # against the authority that supplied the value it just REPLACED, which is the
                # precise failure this flag exists to fix, one level up. So re-stamp it.
                import gate
                gate.record(c, p["id"], "verified", "overwrite:" + str(a.from_), str(a.from_),
                            None, "fields corrected from this authority: " + ", ".join(upd))
        else:
            print(f"  [{p['id']}] nothing missing to fill")
    c.commit()
    if fixed: reindex_lists(c)
    print(f"refreshed {fixed} record(s); run `papers bib` to regenerate references.bib + INDEX.md")


def main():
    ap = argparse.ArgumentParser(prog="papers")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search"); s.add_argument("query", nargs="?", default="")
    s.add_argument("--list"); s.add_argument("--author"); s.add_argument("--year", type=int)
    s.add_argument("--limit", type=int, default=25); s.set_defaults(fn=cmd_search)
    s = sub.add_parser("show"); s.add_argument("id", type=int); s.set_defaults(fn=cmd_show)
    s = sub.add_parser("open"); s.add_argument("id", type=int); s.set_defaults(fn=cmd_open)
    s = sub.add_parser("lists"); s.set_defaults(fn=cmd_lists)
    s = sub.add_parser("list"); s.add_argument("name"); s.set_defaults(fn=cmd_list)
    s = sub.add_parser("add"); s.add_argument("ident"); s.add_argument("--list", action="append")
    s.add_argument("--pdf", action="store_true"); s.add_argument("--no-pdf", action="store_true")
    s.set_defaults(fn=cmd_add)
    s.add_argument("--force-unverified", metavar="REASON", dest="force_unverified",
                   help="admit despite the gate refusing; REASON is recorded in the "
                        "verification table (never a silent bypass)")
    s = sub.add_parser("tag"); s.add_argument("ident"); s.add_argument("--list", action="append")
    s.add_argument("--remove", action="store_true"); s.set_defaults(fn=cmd_tag)
    s = sub.add_parser("rename-list"); s.add_argument("old"); s.add_argument("new")
    s.set_defaults(fn=cmd_renamelist)
    s = sub.add_parser("delete-list"); s.add_argument("name"); s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_deletelist)
    s = sub.add_parser("refresh"); s.add_argument("ident", nargs="?")
    s.add_argument("--overwrite", action="store_true",
                   help="correct fields that are present but disagree with --from; "
                        "without it refresh only fills blanks and can never fix a wrong value")
    s.add_argument("--fields",
                   help="comma-separated field list to limit --overwrite to, e.g. --fields year. "
                        "Authorities disagree field by field: ADS carries the citable issue year "
                        "and truncates pages to the first, Crossref carries the full page range "
                        "and the online-first year. Taking a whole record from either loses "
                        "something, so take the field each is right about")
    s.add_argument("--missing-year", action="store_true")
    s.add_argument("--from", dest="from_", help="explicit bibcode/arxiv/doi to refetch from")
    s.set_defaults(fn=cmd_refresh)
    s = sub.add_parser("ads"); s.add_argument("query"); s.add_argument("--rows", type=int, default=20)
    s.add_argument("--cited", action="store_true"); s.set_defaults(fn=cmd_ads)
    s = sub.add_parser("gaps"); s.add_argument("--fetch-arxiv", action="store_true"); s.set_defaults(fn=cmd_gaps)
    s = sub.add_parser("bib"); s.set_defaults(fn=cmd_bib)
    s = sub.add_parser("stats"); s.set_defaults(fn=cmd_stats)
    s = sub.add_parser("reindex"); s.add_argument("--list"); s.set_defaults(fn=cmd_reindex)
    s = sub.add_parser("index"); s.add_argument("--domain"); s.add_argument("--grep")
    s.add_argument("--anchors", action="store_true"); s.add_argument("--no-refresh", action="store_true")
    s.set_defaults(fn=cmd_index)
    s = sub.add_parser("set-summary"); s.add_argument("name"); s.add_argument("--blurb")
    s.add_argument("--themes"); s.add_argument("--domains"); s.set_defaults(fn=cmd_setsummary)
    s = sub.add_parser("load-summaries"); s.add_argument("file"); s.set_defaults(fn=cmd_loadsum)
    s = sub.add_parser("attach"); s.add_argument("id", type=int); s.add_argument("file")
    s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_attach)
    s = sub.add_parser("audit", help="re-verify every identifier; non-zero exit on regression")
    s.add_argument("--strict", action="store_true",
                   help="also exit non-zero when any record fails to verify, not just on regression")
    s.add_argument("--overrides", action="store_true", help="list recorded overrides and exit")
    s.add_argument("--dry-run", action="store_true", help="do not update the verification table")
    s.add_argument("--limit", type=int, default=0)
    s.add_argument("--workers", type=int, default=6)
    s.set_defaults(fn=cmd_audit)
    s = sub.add_parser("check-refs",
                       help="verify references in a document you did not write (read-only)")
    s.add_argument("file"); s.add_argument("--json", action="store_true", dest="as_json")
    s.add_argument("--strict", action="store_true",
                   help="also exit non-zero on DRIFT and UNCHECKABLE")
    s.add_argument("--offline", action="store_true",
                   help="parse only, no network call (confidential material)")
    s.add_argument("--workers", type=int, default=6); s.set_defaults(fn=cmd_checkrefs)
    s = sub.add_parser("clean"); s.set_defaults(fn=cmd_clean)
    s = sub.add_parser("init", help="set up your profile (and a new library)")
    s.add_argument("--library"); s.add_argument("--email"); s.add_argument("--backend", choices=["claude", "velella"])
    s.add_argument("--yes", action="store_true", help="accept every default, ask nothing"); s.set_defaults(fn=cmd_init)
    s = sub.add_parser("profile", help="show your profile and key fingerprint"); s.set_defaults(fn=cmd_profile)
    s = sub.add_parser("web", help="open the web app (starting it if needed); `web stop` stops it")
    s.add_argument("action", nargs="?", default="open", choices=["open", "stop", "status"])
    s.add_argument("--port", type=int, default=int(os.environ.get("PAPERS_WEB_PORT") or profile_get("port", default=8787)))
    s.set_defaults(fn=cmd_web)
    s = sub.add_parser("serve"); s.add_argument("--port", type=int, default=int(os.environ.get("PAPERS_WEB_PORT", 8787)))
    s.add_argument("--no-open", action="store_true"); s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("sync-trillium"); s.add_argument("--dry-run", action="store_true")
    s.add_argument("--no-pdfs", action="store_true"); s.set_defaults(fn=cmd_synctrillium)
    a = ap.parse_args()
    # check-refs needs no library (and must not have one); init CREATES the library and profile
    # report what is missing; web checks for itself once it knows it has to start a server
    if a.cmd not in ("check-refs", "init", "profile", "web"):
        require_lib()
    # PROPAGATE the command's exit code. `papers audit` returns non-zero on a regression and the
    # scheduled LaunchAgent keys off that; swallowing it here made a detected regression exit 0,
    # i.e. the recurring check would have reported success forever while printing "EXIT 1".
    # Found by testing the exit code rather than the message, 2026-08-14.
    return a.fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
