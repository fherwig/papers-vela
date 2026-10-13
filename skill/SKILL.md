---
name: papers
description: Search, browse, open, and grow your local papers library at $PAPERS_LIB (the app-free replacement for ReadCube/Papers). Use for "find papers on X", "papers about Y in my <list> list", "papers by <author>", "open that paper / open paper N", "what lists do I have", "what's in my <list> list", "add this DOI/arXiv to my library", "fetch the missing PDFs", "regenerate references.bib", "which of my lists cover <topic>", or any query about the personal paper collection. Runs the CLI from the papers-vela repo clone (tools live in the repo; the library holds data only). **Citations are verified against Crossref/ADS/arXiv on admission and a wrong one is refused**, so use this rather than hand-writing a bib entry — hand-written `@article{}`/`\bibitem` is refused fleet-wide by a hook.
---

# papers — your papers-vela library

A self-owned library (DB + PDFs + per-list symlinks + `references.bib`) living at `$PAPERS_LIB`.
The tools live in the **papers-vela repo clone** at `$PAPERS_VELA`. Drive everything through the
CLI below; never edit `library.db` by hand.

**The library comes from the user's profile** (`~/.config/papers/profile.json`, written by
`papers init`; `papers profile` shows it), so you usually need not set `PAPERS_LIB`. Set it only to
override the profile for one run. You still need **`PAPERS_VELA`** (the repo-clone path) to invoke
the CLI by full path — if it's unknown from context, ask. A user with no profile yet: run
`papers init` (it creates the library and asks for the ADS token and chat model).

## Library backends

The library is `$PAPERS_LIB` if set, else `library` in the profile, else the path in
`~/.config/papers/lib`, else the first default location that already contains a `library.db`
(`~/Papers`, `~/papers-lib`).
Auto-discovery only ever selects an **existing** library — creating a new one still needs an
explicit path, so the tool never writes to a directory you didn't name. It errors clearly if none
is found.

**Plain directory** (simplest, writable). Built by `import_bib.py`; PDFs live in the library's
`pdfs/` and `pdf_path` is relative. `papers attach <id> <file>` files an existing local PDF onto a
record (no re-download).

**Zotero-backed library** (one option). Zotero is the single source of truth for papers
and PDFs. `pdf_path` stores absolute `~/Zotero/storage/{key}/filename.pdf` paths — no copying;
both Zotero and papers-vela open the same file. Synced via:

- `zotero/sync.py` — pulls new Zotero items into papers-vela, picks up new Zotero PDFs,
  auto-reindexes. Safe to run while Zotero is open (reads Zotero DB in immutable WAL mode).
  ```
  python3 $PAPERS_VELA/zotero/sync.py --once [--verbose]
  python3 $PAPERS_VELA/zotero/sync.py --watch          # poll continuously
  ```
- `zotero/enrich_bibcodes.py` — populate the `bibcode` column. First harvests "ADS Bibcode:"
  lines already in Zotero's extra fields (free), then queries NASA ADS for the rest.
  `--update-zotero` writes found bibcodes back to Zotero (requires Zotero closed).
  ```
  python3 $PAPERS_VELA/zotero/enrich_bibcodes.py [--zotero-only] [--ads-only] [--update-zotero]
  ```
- `zotero/fetch_pdfs.py` — downloads missing PDFs (ADS resolver → Unpaywall → arXiv),
  saves into Zotero storage, registers as a proper Zotero attachment, updates `pdf_path`.
  **Requires Zotero to be closed** (writes to `zotero.sqlite`).
  ```
  python3 $PAPERS_VELA/zotero/fetch_pdfs.py [--dry-run] [--verbose]
  ```
  Combined in one shot: `python3 $PAPERS_VELA/zotero/sync.py --once --fetch-pdfs --verbose`

**Nextcloud-synced share** (read-only for recipients). Nextcloud sets `-r` permissions on all synced files,
making `library.db` and `pdfs/` unwritable — you can browse and search but cannot add papers,
update records, or reindex. If the user wants a writable library synced via Nextcloud, they need
to create a fresh one with `import_bib.py` in a directory that is not under Nextcloud's sync root.

**Run commands as** (`PAPERS_LIB` optional — omit to auto-discover, or prefix it to override):
`python3 "<user's PAPERS_VELA>/papers.py" <command> [...]`

## Trillium sync

The laptop library can be **bidirectionally** synced — DB *and* PDFs — with a copy on a remote
cluster, set as `remote` (`host`, `path`) in the profile; it was written for a `/scratch` directory
on Trillium. The library is then fully usable (search, open PDFs, add new papers) from a cluster
session too, not just metadata-only. One command, laptop-initiated only (the cluster has no path
back to the laptop):

```
python3 $PAPERS_VELA/papers.py sync-trillium [--dry-run] [--no-pdfs]
```

- **Cheap when nothing changed**: compares a lightweight content fingerprint (row counts + identity
  keys) on both sides first — one local query + one SSH round trip. If they match, it's a no-op.
- **Merges by content identity** (doi > arxiv > citekey > normalized title+year), never by the raw
  `id` column — `id` is a bare autoincrement, not stable across two independently-writable copies,
  which is why earlier attempts to share this file didn't work. Papers new on either side get copied
  to the other; fields empty on one side get filled from the other; if both sides set the *same*
  field to *different* values, both are left as-is and the divergence is printed, not resolved —
  never guesses which side is "right."
  - PDFs sync via plain `rsync --ignore-existing` both directions — safe because PDF filenames are
    deterministic per paper and files are never edited in place, so "copy whatever's missing" can't
    lose data.
- **Purely additive** — never deletes anything on either side, in the DB or on disk.
- First run bootstraps a full push (DB + all PDFs, several GB — expect it to take a few minutes).
  Every run after that only moves the delta.
- Implementation: `trillium_sync.py` in the repo. Keeps one rolling backup of the pre-merge local DB
  at `library.db.pre-trillium-sync.bak` for manual rollback if anything ever looks wrong.
- `/scratch` is scratch space with a purge policy on old/untouched files on most Alliance clusters —
  re-running this sync periodically also refreshes file mtimes, which incidentally protects against
  that. Don't treat the cluster copy as a second archival copy; the laptop's copy remains the
  canonical one.

## Citation correctness — read this before adding or citing anything

Added 2026-08-14 after four bibliography entries with wrong data reached an **accepted** Physical
Review Letters manuscript. Two DOIs resolved to unrelated papers, one denoted a paper that does not
exist, and all four had paraphrased titles and truncated author lists. **The library held the
correct values for two of them.** The entries had been typed by hand into a `.bib`, bypassing this
tool entirely. So the rules below are about the paths *around* the library as much as the library.

**The fact that invalidates the obvious check:** a DOI wrong by two characters **still resolves**,
to a different paper. `10.1093/mnrasl/slw175` is Reindl 2017 (correct); `slw177` is Askar 2017
(wrong). Asking "does this identifier resolve?" passes both. Only comparing the returned metadata
catches it. Never treat a successful resolution as verification.

**The admission gate.** `add` and `import_bib.py` check each record against an authority and refuse
on `mismatch`, `unresolvable`, `unreachable`, or `no-identifier`. Validation follows the hierarchy
**journal (Crossref) > NASA ADS > arXiv**; a record validated by any tier is kept and rebuilt from
the highest tier that validated it, with the provenance written to `notes` and exported as `note={}`
in `references.bib`.

**If the gate refuses, do not work around it.** Fix the identifier, or if the item genuinely cannot
be verified — a book chapter, a thesis, a private communication, an old proceeding — admit it
deliberately:

```
papers add <ident> --force-unverified "why this cannot be verified"
```

That reason is **recorded** in the verification table and listed by `papers audit --overrides`. It
is an audited exception, never a silent bypass. Editing the DB directly to dodge the gate defeats
the only mechanism standing between the library and a repeat of the incident above.

**Never hand-write a citation.** A fleet-wide hook (`no-handwritten-citation.py`) refuses a
hand-authored `@article{}` written into a `.bib` and a `\bibitem` written into a `.tex`, in both
fleets. If you are composing title/author/DOI fields rather than copying them from a fetch you just
ran, you are producing exactly the signature that caused the incident. Do this instead:

```
papers add <doi|arxiv|bibcode>     # fetched from an authority, verified on admission
papers bib                         # regenerate references.bib
```

then `\cite{}` the key it returns. Every other manuscript in the group gets the same correct record.
The hook has one declared exit, `% HAND-CITATION: <reason>` on its own line, which is logged.

### Checking somebody else's references — `check-refs`

Refereeing is where this failure is most likely and least caught: a manuscript under review can
carry the same signature, and no reviewer checks sixty DOIs by hand.

```
papers check-refs <file.bib | file.tex> [--json] [--strict] [--offline]
```

**Read-only, and it never touches the library** — no insert, no tag, no `references.bib` rewrite.
It does not even open `library.db`, and it runs with no library configured at all. Someone else's
bibliography must never become our records by side effect.

| verdict | meaning |
|---|---|
| `ok` | identifier resolves **and** the metadata agrees |
| `MISMATCH` | resolves, but to a **different paper** — the case a human reviewer never catches |
| `UNRESOLVABLE` | identifier 404s; the cited paper may not exist |
| `DRIFT` | right paper, paraphrased title or truncated author list |
| `UNCHECKABLE` | no DOI or arXiv id, so nothing can verify it — reported, never passed |

Exits non-zero on any `MISMATCH` or `UNRESOLVABLE` (`--strict` also fails on `DRIFT`/`UNCHECKABLE`),
so it drops into a referee workflow the way `papers audit` sits in the weekly one. It prints a line
you can paste into a report, which names what the DOI *actually* returns — the useful sentence is
not "reference 17 is wrong" but "reference 17's DOI resolves to Askar et al. 2017, a different
paper".

`.tex` input is verified differently: a `\bibitem` is prose with no fields, so instead of parsing a
title it pulls the DOI out, asks the authority what that identifier is, and checks whether that
author and year actually appear in the entry.

**Confidentiality — say this to the user before checking a manuscript under review.** The check
sends the document's identifiers to Crossref and ADS. Individually they are public; collectively a
reference set can fingerprint a manuscript. `--offline` parses and reports structure with no
network call at all.

## Commands

| Command | What it does |
|---|---|
| `search "<terms>" [--list L] [--author A] [--year Y] [--limit N]` | Smart search; `✓` marks a local PDF, returns `[id]`s. A bare query is parsed: **`Author 2008`** (or a range `Author 2008-2012`) → author+year; **`topic 2007`** → topic terms filtered to that year; otherwise ranked full-text (terms OR-ed, bm25). Explicit `--author/--year/--list` flags force exact filtering instead. |
| `show <id>` | Full record: authors, journal, doi/arxiv, **lists**, pdf path, abstract, and the user's **comments** (marks made in the web GUI's PDF viewer). |
| `open <id>` | Open that paper's PDF in the macOS viewer. |
| `lists` | All lists with paper + PDF counts (and source: cloud / legacy / manual). |
| `list "<name>"` | Papers in a named list. |
| `index [--domain D] [--grep TERM] [--anchors]` | **Routing view** — every list with a 1-line blurb, themes, domains, counts, year span. Filter by `--domain` or `--grep` to find which lists cover a topic. Start here for topical questions. |
| `reindex [--list L]` | Refresh the auto layer (counts, keywords, citation anchors); flags `⚠stale` summaries. (`set-summary` / `load-summaries` store the written blurbs.) |
| `ads "<ADS query>" [--rows N] [--cited]` | Search **NASA ADS** (full query syntax, e.g. `author:"^Herwig" year:2023-2026 abs:i-process`); returns bibcodes + year + citation count. Pipe a bibcode into `add`. |
| `add <doi-or-arxiv-or-bibcode> [--list "L"] [--pdf] [--force-unverified "REASON"]` | Fetch metadata (NASA ADS bibcode / Crossref DOI / arXiv), insert, tag to list(s). **The admission gate can REFUSE this** — see § Citation correctness. **Metadata only by default** — PDFs are fetched on demand (web GUI download button, `gaps --fetch-arxiv`, or `--pdf` to fetch at add time). When a PDF is fetched, the **journal/publisher open-access copy is preferred** over arXiv (version of record). |
| `attach <id> <file> [--force]` | File an **existing local PDF** onto a paper (copy into the library's `pdfs/` with a homogenized name, set `pdf_path`). Use when you already have the PDF — `add` only *downloads*. Plain-directory backend; Zotero manages its own PDFs. |
| `gaps [--fetch-arxiv]` | Current-library papers lacking a PDF; `--fetch-arxiv` pulls the free ones. |
| `bib` | Regenerate `references.bib` + `INDEX.md` + `lists/*.md`. |
| `audit [--strict] [--overrides] [--dry-run] [--limit N]` | Re-verify the library against the authorities; **exits non-zero on any regression**. `--overrides` lists every record admitted by an explicit override. Schedule it weekly (cron or launchd) so drift is caught. |
| `verify.py [--only doi\|arxiv] [--report] [--regrade] [--selftest]` | The verifier itself (run from the repo, not via `papers`). Caches authority responses, so a comparator change re-scores offline with `--regrade` instead of re-fetching. `--selftest` is 52 cases. |
| `check-refs <file.bib\|file.tex> [--json] [--strict] [--offline]` | Verify references in a document **you did not write** (refereeing). **Read-only — never opens the library**, and needs no library configured. Verdicts: `ok` / `MISMATCH` (resolves to a *different* paper) / `UNRESOLVABLE` / `DRIFT` (right paper, paraphrased title or truncated authors) / `UNCHECKABLE` (no identifier). **Exits non-zero on MISMATCH or UNRESOLVABLE.** Warn the user about confidentiality first — see § Citation correctness; `--offline` skips all network. |
| `stats` | Library overview. |
| `serve [--port N] [--no-open]` | Launch the localhost web GUI (browse + a chat that searches the library and ADS and can add papers through the gate). Tell the user to open the printed `http://127.0.0.1:…/?token=…` URL; `Ctrl-C` stops it. |
| `sync-trillium [--dry-run] [--no-pdfs]` | Bidirectionally merge `library.db` + `pdfs/` with the profile's `remote` (e.g. a cluster's `/scratch`; laptop-initiated only). See § Trillium sync above. |

## How to use it (for Claude)

**Routing first (especially for the encyclopedia chapter).** For any *topical* request — "what do
we have on X", "which lists are relevant to <chapter section>", "find material across lists about
Y" — run `papers index` (optionally `--domain D` / `--grep TERM`) **first**. It returns a blurb +
themes + domains per list, so you can pick the relevant lists and then drill in with `list` /
`search` — without scanning the whole library. **`papers index` auto-refreshes the auto layer on
every call** (and `add`/`gaps` self-refresh too), so counts/keywords/anchors are always current —
nobody has to remember to reindex. `⚠stale` / `⚠no-blurb` means a list's membership changed since its
**blurb** was written: when that list is relevant to the user's request, re-read it and update the
blurb with `papers set-summary "<list>" --blurb "…" --themes "…" --domains "…"`.

1. **Finding papers** → run `search`, optionally with `--list`/`--author`/`--year`. Show the
   user the returned rows (id, year, author, title). To open one, run `open <id>`.
2. **"What's in my <list>"** → `list "<name>"` (use `lists` first if the exact name is unclear).
3. **Searching ADS (the usual front door)** → `ads "<query>"`, then import a hit with
   `add <bibcode> --list "<list>"`, then `bib` to refresh `references.bib`. arXiv IDs look like
   `2401.01234`; DOIs start with `10.`; ADS bibcodes are 19 chars (e.g. `2023MNRAS.525.1601H`).
   ADS token: `~/.config/ads/token`.
4. **Filling missing PDFs** → `gaps` to see them, `gaps --fetch-arxiv` to grab arXiv ones.
5. Filenames are `LastName_Year[abc]_keywords.pdf`; a paper can be in many lists (browse
   `$PAPERS_LIB/by-list/<list>/`). `references.bib` carries `file=` links for JabRef.

Prefer one CLI call, read its output, then act (e.g. `search` → `open`). Keep the user's
exact list names (they can contain spaces, `&`, `/`).
