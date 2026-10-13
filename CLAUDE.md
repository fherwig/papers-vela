# CLAUDE.md — papers-vela

Guidance for Claude Code (and contributors) working in this repo.

**Naming.** The project is **papers-vela** (renamed from papers-agent, 2026-10-09). It is a *vela*,
not an agent: in this group "agent" means a rostered identity with a charter, and this app has none.
Don't describe it or its chat as an agent. The command (`papers`), skill (`/papers`), config
(`~/.config/papers/`) and `PAPERS_*` variables keep their names; `PAPERS_AGENT` became `PAPERS_VELA`
(the old variable is still read). The folder on disk and the GitLab project are still `papers-agent`.

**Repositories and license.** Development happens in the group's GitLab (`csa/papers-agent`: full
history, merge requests). The public repository, <https://github.com/fherwig/papers-vela>, started
from a fresh history and gets one squashed commit per release whose tree is exactly GitLab `main`'s
(see **Publishing to GitHub**). So **everything on `main` is public**: no personal paths, accounts or
hosts, no manuscript or referee details, no data. License: PolyForm Noncommercial 1.0.0 (`LICENSE`);
the vendored pdf.js keeps its Apache-2.0 license.

## What this is
A **vela** for a research library: a secure, governed space for AI-augmented work with your
papers. Underneath, an own-your-data reference manager: a local SQLite + PDF library with full-text and NASA
ADS search, `references.bib` export, a self-refreshing per-list routing index, a localhost **web
GUI** (`papers serve`: browse + an isolated chat on the engine's tools), and a Claude Code `/papers` skill. A
scriptable alternative to Mendeley/ReadCube. See `README.md`.

## Structure: tools here, data in the library
Two things, kept apart. **Tools = this repo** — the single source of truth, in git. **Library = a
separate data folder** at `$PAPERS_LIB` (`library.db`, `pdfs/`, generated `references.bib` /
`INDEX.md` / `lists/`): data only, never code. The repo is pointed at a library via `PAPERS_LIB`;
one repo serves any number of libraries. **Do not put a copy of the tools inside a library** — an
earlier `tools/` copy in the library drifted out of sync and was removed (2026-06-22). The installed
`/papers` skill is a **symlink** to `skill/SKILL.md` here, so it can never drift.

## Hard rule: ship the tool, never the data
This repo contains **code only**. Never commit anyone's `library.db`, PDFs, `references.bib`,
`INDEX.md`, list exports, or API tokens — `.gitignore` excludes them; keep it that way. PDFs are
copyrighted and personal libraries are private.

## Layout
- `papers.py` — the CLI and engine (search/show/open/lists/list/ads/add/attach/tag/rename-list/
  delete-list/refresh/gaps/index/reindex/set-summary/load-summaries/bib/stats/clean/serve/
  sync-trillium). Read commands split into `query_*` (return data) + thin printers
  so the web server reuses the same SQL. `bib`, `serve` and `sync-trillium` are thin
  dispatchers that hand off to `generate_outputs.py`, `web/server.py`, `trillium_sync.py`.
- `import_bib.py` — universal BibTeX → `library.db` on-ramp (creates the schema).
- `zotero/sync.py` — keep papers-vela in sync with a live Zotero library: pulls new items,
  picks up newly attached PDFs, auto-reindexes. Safe while Zotero is running (immutable WAL read).
- `zotero/enrich_bibcodes.py` — populate the `bibcode` column: first harvests "ADS Bibcode:"
  lines already in Zotero's extra fields (no API cost), then queries NASA ADS for the rest.
  Optionally writes found bibcodes back to Zotero (`--update-zotero`, requires Zotero closed).
  When DOI and arXiv lookups both fail, falls back to a **verified** title/author/year search:
  the hit is accepted only if the ADS record independently agrees on title, first-author
  surname, and year — otherwise it returns nothing rather than guessing. Retries on an
  accent/punctuation-stripped title (ADS's phrase parser chokes on `(p,γ)` and umlauts), and
  prefers the most citable record when several match (article > eprint > proceedings >
  abstract). Disable with `--no-title-fallback`. The Zotero harvest matches on DOI **and**
  normalised title, so records storing a URL (or nothing) in `doi` still pick up a bibcode
  Zotero already holds instead of costing an ADS call. Note the write-back only covers
  bibcodes found *during that run* and matches Zotero by DOI, so it cannot backfill an
  existing gap.
- `zotero/fetch_pdfs.py` — download missing PDFs (ADS resolver → Unpaywall → arXiv),
  save into Zotero storage, register as a proper Zotero attachment, update `pdf_path`.
  Requires Zotero to be closed (writes directly to `zotero.sqlite`).
- `zotero/browser_fetch.py` — last-resort PDF fetch through the user's OWN authenticated
  browser session, for paywalled / Cloudflare-walled version-of-record PDFs that curl can't get.
  Opens the publisher PDF URL in a new browser window (the browser's CLI, reusing the live profile's
  cookies/Cloudflare clearance), carves the PDF out of the browser disk cache, then closes that
  window via an Apple event — no Accessibility/keystroke permissions. Browser is **user-configurable**
  via `PAPERS_BROWSER` (default `firefox`) + `PAPERS_BROWSER_BIN`; each browser is a backend class
  (only Firefox implemented — adding Chrome/Safari = one new backend). Disable with
  `PAPERS_BROWSER_FETCH=0`. Wired as a fallback into `fetch_pdfs.fetch_pdf` (before the arXiv
  preprint fallbacks, so the published PDF wins) and `upgrade_arxiv_pdfs.fetch_publisher_pdf`.
  Note: APS PDF URLs are case-sensitive in the journal token; the DOI-derived URL restores the case,
  and the plain DOI link is used as the generic fallback for publishers without a known PDF pattern.
- `zotero/backfill_metadata.py` — fill in missing `journal`/`volume`/`issue`/`pages` for papers
  already in papers-vela. Queries ADS by bibcode (or DOI if no bibcode). Skips records where ADS
  only has an arXiv record (not yet published). `--update-zotero` also patches Zotero item fields
  (requires Zotero closed). Run this after any bulk sync or when the GUI shows blank journal fields.
- `zotero/check_sync.py` — **read-only audit**: compare papers-vela against Zotero and report
  MISSING, DOI-DRIFT, PDF-DRIFT, NO-PDF, ORPHAN without changing anything. Useful for inspection.
  For actual fixes, `sync.py --full` handles everything automatically.
- `zotero/sync.py --push-orphans` — **reverse sync**: push PA records that have no Zotero
  counterpart into Zotero as new `journalArticle` (or `preprint`) items, then reconcile back.
  Requires Zotero **closed** (writes `zotero.sqlite`). Idempotent — safe to re-run.
- `generate_outputs.py`, `rebuild_bylist.py` — regenerate `references.bib`, `INDEX.md`, `by-list/`.
- `trillium_sync.py` — bidirectional laptop ↔ Trillium `/scratch` sync of `library.db` + `pdfs/`
  (`papers sync-trillium`). See **Trillium sync** below.
- `web/` — the localhost web GUI (`papers serve`): stdlib `ThreadingHTTPServer` + no-build SPA;
  `web/chat_tools.py` is the chat's own stdio MCP server (its only tools).
  Design + security model in `web/README.md`; see **Web GUI invariants** below.
- `skill/SKILL.md` — the Claude Code `/papers` skill (self-contained invocation; installed as a symlink).
- `migration/` — reference scripts for migrating off ReadCube/Papers 3 (user-specific paths).
- `docs/web-gui-plan.md` — web GUI design record + build decisions (shipped).

## Library backends
`resolve_lib()` (top of `papers.py`) locates the library in this order — **`PAPERS_LIB` is optional,
not required**:

1. `$PAPERS_LIB` — explicit; used as-is.
2. The profile's `library` (written by `papers init`, which is how a new library is created).
3. `~/.config/papers/lib` — a file holding one path (the older per-user default).
4. First of `DEFAULT_LIB_CANDIDATES` (`~/Papers`, `~/papers-lib`) that already contains a
   `library.db`.

Auto-discovery only ever selects an **existing** `library.db`, so the tool never creates a library
in a directory nobody named; `require_lib()` errors only when all four fail. `~/papers-lib` is in
the candidate list because that's the library path on Trillium — see **Trillium sync**.

Three supported backends, none privileged:

- **Plain directory** — built by `import_bib.py`; writable; PDFs live in the library's `pdfs/` and
  `pdf_path` is relative (`pdfs/...`). The simplest backend; `papers attach <id> <file>` files a
  local PDF onto a record.
- **Zotero-backed** — Zotero owns the PDFs; `pdf_path` stores absolute `~/Zotero/storage/{key}/…`
  paths so both Zotero and papers-vela open the same file. Kept in sync via the `zotero/` scripts.
- **Nextcloud-synced share** — a directory synced via Nextcloud. A *recipient* of a read-only share
  gets `-r` permissions (browse/search/read only; no add/reindex); the owner's copy is writable.

Do not hardcode any specific path as "the" library in shared code or docs — it's per-user config.

**Profile.** Everything about one user lives in `~/.config/papers/profile.json` (or
`$PAPERS_PROFILE`): `library`, `email`, `ads_token_file`, `chat` (backend `claude` | `velella` |
`openai`, model, url, key_file), optional `remote` (the `sync-trillium` host and path), optional
`port`. `papers init` writes it; `papers profile` shows it. Environment variables still win. No
personal path, account or host belongs in the code; put it in the profile. `generate_outputs.py`
and `rebuild_bylist.py` now take the library from `papers.LIB` like everything else (the old
"known drift", where `papers bib` needed `PAPERS_LIB` set, is fixed).

## Upgrading arXiv preprint PDFs to journal versions

Run `zotero/upgrade_arxiv_pdfs.py` (requires Zotero **closed**) to find papers whose DOI is
`10.48550/arxiv.*` but whose ADS bibcode points to a real journal record, then fully upgrade them:

```bash
PAPERS_LIB=~/Zotero/papers-vela PAPERS_VELA=~/papers-vela PAPERS_EMAIL=you@example.com \
  python3 zotero/upgrade_arxiv_pdfs.py --verbose
```

**What it does per paper (on success):**
1. Downloads the journal PDF (see priority below)
2. Deletes **all** existing PDF attachments from the Zotero item (files + DB rows) — enforces one PDF
3. Converts the Zotero item type from `preprint` → `journalArticle` (typeID 31 → 22); removes
   preprint-only fields (`repository`, `archiveID`, `genre`)
4. Updates Zotero metadata: DOI (journal), URL (`https://doi.org/…`), `publicationTitle`,
   `libraryCatalog`, `volume`, `issue`, `pages`, `date`; sets `ADS Bibcode:` in `extra`
5. Registers the new PDF as the sole Zotero attachment
6. Updates papers-vela DB: `doi`, `journal`, `volume`, `issue`, `pages`, `pdf_path`

**Download priority:**
1. `ADS-gateway` — `https://ui.adsabs.harvard.edu/link_gateway/{bibcode}/PUB_PDF` with
   `Authorization: Bearer {token}` (from `~/.config/ads/token`). Confirmed working for
   **A&A (EDP Sciences/ESO)** and — as of 2026-07-27 — for **IOPscience (ApJ/PSJ)**: 3/3
   succeeded, returning genuinely typeset version-of-record PDFs. This reverses an earlier
   note here that IOP was blocked 0/7; IOP evidently stopped blocking the gateway, so try it
   before assuming a publisher needs the browser fallback.
   - **MNRAS/Oxford Academic** — gateway redirects to `academic.oup.com` which returns 403
   - **Overlay journals (OJAp)** — no publisher PDF exists at all; arXiv *is* the version of
     record. `upgrade_arxiv_pdfs.py` skips these via `_OVERLAY_JOURNALS` rather than retrying.
2. ADS direct esource URLs (fallback for journals not behind bot protection)
3. Unpaywall publisher-hosted copy (`host_type == "publisher"` only, no repository/arXiv fallback)

**Oxford Academic (MNRAS) note:** Oxford blocks all programmatic access including curl with browser
UA headers, even for gold OA papers that Unpaywall marks as `is_oa: True` with a `host=publisher`
URL. The URL is real; the block is JS-challenge bot protection. Workaround: use Zotero's
"Find Available PDF" from within the app — the Zotero connector runs inside your browser and
inherits its authenticated/cookie session, bypassing the bot check.

**ADS resolver notes:** The correct endpoint is `/esource` (singular, not `/esources`). Link types
are returned as `ESOURCE|PUB_PDF` — strip the prefix before matching. Both handled in
`ads_esource_urls()` in `zotero/fetch_pdfs.py`.

## Adding a paper into Zotero (papers-vela → Zotero)

The normal flow is Zotero → papers-vela (`sync.py`). The reverse case: a record exists in
papers-vela with `source='added'` (added straight to PA, e.g. via `papers add`) and is **missing
from Zotero**. Since Zotero is the source of truth, push it into Zotero, then sync back.

Add via the **Zotero connector API** (works whenever Zotero is running — it listens on port
23119; no setting needed, unlike the data API below):

```bash
# metadata from Crossref → POST to the connector. One item per request.
curl -s "https://api.crossref.org/works/<DOI>" ...        # build a Zotero item dict
curl -s -X POST "http://localhost:23119/connector/saveItems" \
     -H "Content-Type: application/json" \
     -d '{"items":[<item>],"uri":"https://doi.org/<DOI>","sessionID":"<unique>"}'
```

Gotchas (learned the hard way):
- **Clean the title (and other text fields) first.** Crossref returns titles full of HTML/MathML
  (`<mml:math …>`, `<sup>`, `<i>`) and LaTeX `$…$`; pushing them raw stores dirty titles in Zotero.
  Run every field through `clean_text()` from `papers.py`, or — when the paper already exists in
  papers-vela — reuse PA's already-clean stored title instead of re-fetching from Crossref.
- The read/write **data API** is read-only here: POST to `/api/users/0/items` returns `400 Endpoint
  does not support method` and PATCH/PUT/DELETE return `501 Not Implemented`. Only the connector can
  create; it cannot update or delete. **To edit/clean an existing item's fields, write
  `zotero.sqlite` directly with Zotero closed** (the `set_field` itemDataValues/itemData pattern in
  `upgrade_arxiv_pdfs.py`); to attach an existing local PDF, reuse `insert_zotero_attachment()` in
  `fetch_pdfs.py`.
- **One item per request, each with a unique `sessionID`** — reusing a session across items
  returns `409 Conflict`.
- After the items exist, reconcile: `sync.py --once` picks up new items and links their attachments.
  If a PDF/title changed on an *already-synced* item, the incremental run skips it — use
  `sync.py --once --full` to force a full reconcile.

## After any manual Zotero changes

A single command keeps papers-vela fully in sync with Zotero — new items, updated PDFs, DOI
upgrades, and missing metadata are all handled in one pass:

```bash
# Full reconciliation: new items + pdf/doi/metadata drift + orphan report
PAPERS_LIB=~/Zotero/papers-vela PAPERS_VELA=~/papers-vela \
  python3 zotero/sync.py --once --full --verbose

# Backfill any journal/volume/issue/pages that Zotero itself doesn't have (uses ADS)
# (add --update-zotero to also patch Zotero item fields — requires Zotero closed)
PAPERS_LIB=~/Zotero/papers-vela PAPERS_VELA=~/papers-vela \
  python3 zotero/backfill_metadata.py --verbose
```

`sync.py --full` reconciles every existing record, not just new ones:

| What it fixes | Rule |
|---|---|
| New Zotero item not yet in PA | Insert |
| `pdf_path` stale or missing | Always use Zotero's current attachment |
| DOI drift (arXiv → journal) | Zotero's journal DOI wins; never downgrade |
| `title`, `authors`, `abstract`, `year`, `journal`, `volume`, `issue`, `pages` | Zotero wins whenever it has a non-empty value |
| Orphan (PA record, no Zotero item) | Reported only — run `--prune-orphans` to delete |

`--prune-orphans` requires `--full` and deletes PA records whose Zotero item has been removed.
**Before using `--prune-orphans`, investigate each orphan** — a PA record appearing as an orphan
is not always a deleted Zotero item. The most common false-positive: two PA records share the same
DOI. `build_dedup_maps` only keeps the last one in the `by_doi` map, so the earlier one is never
matched and surfaces as an orphan. Diagnosis:

```python
# Check if an "orphan" is actually a PA duplicate
sqlite3 library.db "SELECT id, citekey, doi FROM papers WHERE doi='<the_doi>'"
# If two rows appear → it's a PA duplicate, not a deleted Zotero item.
# Keep the record whose citekey matches Zotero; delete the other.
```

`check_sync.py` is still available as a **read-only audit** if you want to inspect the state
without changing anything.

## Finding and removing duplicates

### Zotero duplicates (requires Zotero closed)

```bash
sqlite3 ~/Zotero/zotero.sqlite "
SELECT idv.value as doi, COUNT(*) as n,
       GROUP_CONCAT(i.key,', ') as keys,
       GROUP_CONCAT(COALESCE(ck.value,'?'),', ') as citekeys
FROM items i
JOIN itemData id2 ON id2.itemID=i.itemID AND id2.fieldID=59
JOIN itemDataValues idv ON idv.valueID=id2.valueID
LEFT JOIN itemData ckd ON ckd.itemID=i.itemID AND ckd.fieldID=64
LEFT JOIN itemDataValues ck ON ck.valueID=ckd.valueID
WHERE i.itemID NOT IN (SELECT itemID FROM deletedItems)
  AND i.itemTypeID NOT IN (3,14)
GROUP BY LOWER(idv.value) HAVING COUNT(*)>1"
```

To delete the duplicate (keep the clean-citekey item; delete the `a`-suffixed one):
1. Find the duplicate's itemID and its attachment itemID + storage key
2. Insert both into `deletedItems` and the attachment key into `storageDeleteLog`
3. `rm -rf ~/Zotero/storage/<att_key>` (only if the keeper uses a **different** storage key)

### papers-vela duplicates

```bash
sqlite3 library.db "
SELECT lower(doi), COUNT(*), GROUP_CONCAT(id), GROUP_CONCAT(citekey)
FROM papers WHERE doi IS NOT NULL AND doi != ''
GROUP BY lower(doi) HAVING COUNT(*)>1"
```

For each pair: check list memberships of both, transfer any unique lists to the keeper, then
`DELETE FROM paper_lists WHERE paper_id=<old>` and `DELETE FROM papers WHERE id=<old>`.

## Search behavior (CLI, web box, and chat retrieval all share it)

The query understanding lives in `smart_search` → `fts_ranked` / `papers_by_author` in `papers.py`;
the tier weights and rules are documented in those docstrings (the single source of truth — don't
duplicate them here). The two design intents a contributor must not accidentally remove:

- **A bare surname is an author lookup, not a topic search.** `papers_by_author` matches the
  surname as a whole token (text before the comma in each `Last, First` entry), so `Issa` does
  **not** match `Krissansen-Totton`/`Pelissard`. Results are first-author papers newest-first, then
  co-authored newest-first. Both the bare-surname and `Author YYYY` paths route through this helper.
- **Topic search is tiered:** exact phrase (title > anywhere) > first-author > column-weighted bm25
  (title & authors boosted) > recency. Prefix/live mode does no stopword filtering and splits
  hyphens (FTS5 reads `-` as NOT), so `the` and `s-process` both return matches.

## Citation correctness: the gate, the audit, and the hook

Added 2026-08-14 after four wrong entries reached an accepted manuscript. **The
library was not the source** — it held the correct DOIs for two of them; the manuscript's `.bib`
was hand-written. So the work has two halves: prove the library, and close the path that bypasses
it.

**The fact that shapes every check here:** a DOI wrong by two characters *still resolves*. It just
resolves to a different paper.

    10.1093/mnrasl/slw175 -> Reindl 2017, Stingray Nebula     (library value, CORRECT)
    10.1093/mnrasl/slw177 -> Askar 2017, MOCCA-SURVEY BHs     (manuscript value, WRONG)

A "does the identifier resolve?" check passes both. Only comparing the *returned metadata* against
the record catches it. Never write a check that stops at resolution.

- `verify.py` — compares records against Crossref/arXiv. `--selftest` (52 cases) is the contract;
  every comparator rule that was ever wrong on live data is pinned there as a regression case.
  Results cache the authority's raw response, so `--regrade` re-scores the whole corpus offline
  after a comparator change instead of re-fetching (a full pass is ~2,000 network calls).
- `gate.py` — admission validation for `papers add` and `import_bib.py`. **Prohibition** with one
  recorded override (`--force-unverified "<reason>"`, logged to the `verification` table). Its
  `--selftest` uses that manuscript's four real failure modes as live negative controls.
- `resolve.py` — validates against a **source hierarchy: journal (Crossref) > NASA ADS > arXiv**.
  A record validated by any tier stays and is rebuilt from the highest tier that validated it;
  disagreements are written to the record's `notes` as `[provenance] …` and exported as `note={}`
  in `references.bib`. Records no source validates are removal candidates — exported to a restore
  file *before* deletion.
- `papers audit` — re-runs verification, compares against the stored baseline, **exits non-zero on
  regression**. Meant to run weekly from cron or launchd, so a regression surfaces on its own.
- `hooks/no-handwritten-citation.py` — PreToolUse(Write|Edit) guard refusing hand-authored
  `@article{}` in `.bib` and `\bibitem` in `.tex`. Escape hatch `% HAND-CITATION: <reason>`,
  logged. Exempts `references.bib`, anything under the library, and `.bbl` (compiler output).

### Rules a contributor must not undo

- **A source that could not be ASKED is not a verdict.** ADS allows 5,000 queries/day. If a quota
  rejection is reported like "no match", every record after the quota runs out looks checked and
  found wanting — and under the removal policy that means deleted. `SourceUnavailable` and the
  `RECHECK` outcome exist for exactly this; never collapse them into "not found".
- **Normalise both sides identically.** Every false positive this campaign produced came from
  one-sided normalisation: `ø` stripped on our side but not theirs, LaTeX `\'{i}` eating the
  vowel, `$…$` deleted while `<i>` was merely unwrapped. When you touch `norm_*`, apply it to the
  authority too and add the pair to the selftest.
- **Guards must demand positive evidence.** The first ADS matcher skipped its author and year
  checks when those fields were empty, so it confidently matched a record titled *"The
  Astrophysical Journal"* to an 1895 paper. A record too thin to check is "no match", never a match.
- **Test the exit code, not the message.** `papers audit` printed "EXIT 1" while exiting 0 because
  `main()` dropped the return value; the weekly job would have reported success forever.

## Web GUI invariants (`web/server.py`)

Two tiers, and the boundary between them is the whole design — `web/README.md` has the full model.
What a contributor must not quietly weaken:

- **Browse is reads through the engine.** Every endpoint calls the same `query_*` function the
  CLI uses (`import papers as eng`), so GUI and CLI can never disagree. Don't write fresh SQL in the
  server; add a `query_*` to `papers.py` and call it. Its writes are human-initiated and few: the
  Download-PDF button, and **comments** (marks made in the PDF viewer), both refused on a read-only
  library.
- **Marks are the paper's comments, in `library.db` — never in the PDF.** The viewer
  (`web/static/pdfview.js`, ported from the session-dashboard kit, pdf.js vendored under
  `web/static/vendor/pdfjs/`) saves each mark through `papers.add_comment`: page, the QUOTE, the
  note, and rects in page space at scale 1. Writing into the PDF would break the Trillium rsync
  (it relies on PDFs never being edited in place) and the Zotero backend (Zotero owns those files).
  `papers show` and the chat's `show_paper` list them. `trillium_sync` does not carry comments yet.
- **Two chat backends, one frame.** The profile's `chat.backend` picks `claude` (`run_claude`: the
  user's own Claude subscription via `claude -p`) or `velella`/`openai` (`run_openai`: an
  OpenAI-compatible endpoint, tokens.velella.ca by default, tool loop run in the server). Both share
  `stream_chat`'s frame: the server's retrieval, today's date in the context (an open-weight model
  otherwise searches "new" papers in its training year), the add gate, the SSE stream and the log.
  The add gate is `asks_to_add`: `explicit` (the user asked) or `confirm` (a short yes to the
  chat's own offer); a `confirm` turn may add ONE paper (`budget`, enforced in `call_tool`, because
  Qwen told "yes" to three offers added all three). An open model that thinks until `max_tokens`
  with no answer gets that round retried once without thinking.
- **The chat's tools are the engine and nothing else.** `claude -p` runs with `--tools ""`,
  `--strict-mcp-config` (only `web/chat_tools.py`), `--setting-sources local` in an empty cwd and
  `--disable-slash-commands`: no built-ins, no user plugins/skills/hooks/CLAUDE.md, no other MCP
  server. Name what is *allowed*; a deny-list of built-ins leaked the user's MCP tools (a plugin's
  web search answered in the "read-only" chat, 2026-10-06). `chat_tools.py` wraps `query_*`, ADS
  search and `papers add` — never a shell, file read or URL fetch. Its one write, `add_paper`, runs
  the real `papers add` (gate included, never `--force-unverified`) and is offered only when the
  library is writable and the user's own message asks (`ADD_INTENT`) or answers the chat's own
  offer to add with a short yes (`asks_to_add`). Every turn is appended to a local JSONL log,
  `~/.cache/papers-web/chatlog/chat-YYYY-MM-DD.jsonl` (message, server context, each tool call and
  result, answer, cost): read it to see what the chat actually did. `PAPERS_WEB_CHATLOG=0` disables.
- **Three independent access layers:** loopback-only bind, per-launch token (`secrets.compare_digest`)
  on every `/api/*` and `/pdf/*`, and a `Host`/`Origin` allowlist against DNS-rebinding. Each stands
  alone — don't drop one because another covers the case.
- Env knobs: `PAPERS_WEB_PORT` (8787), `PAPERS_WEB_MODEL`, `PAPERS_WEB_CHAT_CONCURRENCY`,
  `PAPERS_WEB_PAPER_CHARS`, `PAPERS_WEB_CHATLOG`. `serve` writes its URL+token to `~/.cache/papers-web/url-<port>` so a
  launcher can reopen a running instance.

## Trillium sync (`trillium_sync.py`, `papers sync-trillium`)

Merges `library.db` + `pdfs/` between the laptop's canonical library and the profile's `remote`
(`host`, `path`; written for a `/scratch` copy on the Trillium cluster). **Laptop-only** — the
cluster has no outbound path back.
Deterministic; no LLM reasoning at merge time. The invariants that make it safe:

- **Rows match by content identity** (`doi` > `arxiv` > `citekey` > normalized `title+year`), never
  by the `id` column — `id` is a local autoincrement and is not stable across two independently
  writable copies. Matching on `id` is exactly what broke earlier attempts at sharing the DB.
- **Coalesce, never clobber.** A field in `PLAIN_FIELDS` is filled in only when one side has it and
  the other is empty. Two different non-empty values = divergence *reported*, never auto-resolved.
- **PDFs move via `rsync --ignore-existing` both ways.** Safe only because PDF filenames are
  deterministic and files are never edited in place. Nothing is ever deleted on either side.
- **Fingerprint short-circuit first** (`FP_SQL`: row counts + identity keys, no content hash). If
  both sides match, the whole sync is a no-op costing one local query + one SSH round trip. Its
  delimiters are printable `<F>`/`<R>` on purpose — `char(30)`/`char(31)` were observed getting
  mangled between macOS's `sqlite3` and Trillium's, producing false divergence reports.
- Requires SSH to the remote without a prompt (on a cluster with MFA, an open auth window).

## Testing and verification
There is **no test suite, no build, and no lint config** — the repo is stdlib-only Python 3.9+, run
directly. Verify changes by exercising the CLI against a scratch library rather than a real one:

```bash
export PAPERS_LIB=/tmp/papers-scratch
python3 import_bib.py some.bib          # creates the schema + imports
python3 papers.py stats                 # sanity check
python3 papers.py search "some topic"   # exercise smart_search
python3 papers.py bib                   # regenerate references.bib + INDEX.md
```

Selftests, each the contract for its module: `python3 verify.py --selftest` (offline),
`python3 hooks/no-handwritten-citation.py --selftest` (offline), `python3 gate.py --selftest` and
`python3 check_refs.py --selftest` (live: they ask Crossref).

Destructive/network paths (`sync-trillium`, the `zotero/` writers) all take `--dry-run` or a
read-only audit mode (`check_sync.py`) — use those before running them for real.

**Schema is defined in two places.** `SCHEMA` in `import_bib.py` creates every table, but
`list_summary` is *also* created by `ensure_summary_table()` in `papers.py` (so the auto layer works
on libraries built before it existed), and `comments` by `ensure_comments_table()` (`COMMENTS_SQL`).
Adding a column to either means editing both.

## Conventions
- Config via env: `PAPERS_LIB` (library dir — optional, see **Library backends**) and
  `PAPERS_VELA` (repo clone dir, needed to invoke the CLI by full path). `PAPERS_EMAIL` for the
  Crossref/Unpaywall polite pool. ADS token at `~/.config/ads/token` (0600). Secrets never live in
  the repo.
- Python 3.9+, standard library only; optional `poppler`/`pdftotext` for PDF text matching.
- The auto layer (`papers reindex`) is refreshed automatically by `index`/`add`/`gaps`; only the
  LLM-written list blurbs need manual refresh (they self-flag `⚠stale`).
- **Zotero run-state is part of the workflow — open or close the app as the task needs, no approval
  required.** The connector API (`localhost:23119`) needs Zotero **running**; scripts that write
  `zotero.sqlite` (`fetch_pdfs.py`, `upgrade_arxiv_pdfs.py`, direct field/attachment edits) need it
  **closed**. Quit with `osascript -e 'quit app "Zotero"'`, reopen with `open -a Zotero`, then sync.

## Publishing to GitHub
GitLab `main` is the working branch; GitHub gets a squashed snapshot of it. After pushing GitLab:

```bash
git fetch github
c=$(git commit-tree "main^{tree}" -p github/main -m "<what this release changes>")
git push github "${c}:refs/heads/main"    # ${c}: zsh reads "$c:r…" as a modifier
```

`commit-tree` reuses `main`'s exact tree, parented on the previous public commit: nothing is checked
out, the two can't drift in content, and GitLab's history (authors' addresses, intermediate states)
never reaches GitHub. Before publishing, grep `main` for personal paths, hosts and accounts.

## Maintainer notes
The maintainer's private coordination, todos and session-end protocol live in the gitignored
`_project.md` (anything starting with `_` is ignored). Personal working files are excluded in
`.git/info/exclude`, not `.gitignore`, so the public ignore list stays generic.
