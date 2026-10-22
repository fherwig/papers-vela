# papers-vela — Web GUI build plan

_Status: **M1 (browse) + M2 (chat) implemented 2026-06-21** — see `web/` and `web/README.md`.
This doc is kept as the design record; the decisions taken during the build are noted in the
"Build decisions" section at the end. M3/M4 (polish, annotations) remain future work._

A portable, **localhost** web GUI over the existing papers-vela library: a chat box for agentic
Q&A over your papers, **markdown-rendered** answers, and a **PDF viewer** with a sidebar of lists
and open-paper tabs. Goal: a lightweight, own-your-data, Mendeley-style front end **with a chat
brain** — something off-the-shelf managers don't have.

## Key decision: drive chat with `claude -p`, not the Anthropic API

Use Claude Code **headless mode** as the chat backend. The `/chat` endpoint shells out to
`claude -p` (print mode → stdout). This:
- needs **no Anthropic API key** and incurs **no separate API billing** — it runs on the existing
  Claude Code auth/subscription;
- **reuses the whole agent** — the `/papers` skill, Bash, the routing index, MCP — so it can
  actually *search / open / add*, not just chat. No tool-use re-implementation.

Confirmed available (Claude Code 2.1.185): `-p/--print`, `--output-format text|json|stream-json`,
`--input-format`, `--allowedTools`/`--disallowedTools`, `--permission-mode`, `--append-system-prompt`,
`-c/--continue`, `--resume`, `--session-id`, `--no-session-persistence`.

(Alternative if a library API is preferred over shelling out: the **Claude Agent SDK** wraps the
same headless mode via `query()`.)

## Architecture

```
papers.py · library.db · pdfs/            ← engine + data (exists)
        │
  local HTTP server (127.0.0.1 only)       ← web/server.py
   GET  /api/lists                         papers.py lists/index  → JSON
   GET  /api/search?q&list&author&year     papers.py search       → JSON (ids, ✓pdf)
   GET  /api/index?domain&grep             papers.py index        → routing view
   GET  /api/paper/<id>                     papers.py show         → record + lists
   GET  /pdf/<id>                           stream the file at pdf_path
   POST /api/chat  {message, session_id}    spawn `claude -p`, stream stdout (SSE)
        │
  single-page UI (web/static/, no build step)
   ├─ left   : lists (with blurbs/domains from the routing index) + open-paper tabs
   ├─ center : chat input + markdown transcript (marked.js + DOMPurify)
   └─ right  : PDF viewer (browser-native <embed> first; PDF.js later for highlights)

  launch:  `papers serve`  → starts server, opens http://127.0.0.1:8787
```

## Chat endpoint detail
- Per turn: `printf '%s' "$msg" | claude -p --output-format stream-json --resume "$sid" \
  --allowedTools <scoped> --permission-mode acceptEdits --append-system-prompt "<librarian>"`.
- Keep **one resume session-id per browser chat** (generate a uuid, pass `--session-id`, reuse with
  `--resume`) so context persists across turns.
- Parse the `stream-json` event stream; forward assistant text deltas to the browser via **SSE**;
  render markdown incrementally.
- Librarian system prompt (sketch): *"You are the papers librarian for the local library at
  $PAPERS_LIB. Use papers.py (search/index/show/list) to answer. Cite paper ids; when you reference
  a paper, surface its id so the UI can offer to open the PDF. Be concise."*
- Make answers actionable: have the model include paper `id`s; the UI linkifies `[id]` → loads
  `/pdf/<id>` in the viewer as a new tab.

## Security (critical — an LLM with Bash behind an HTTP port)
- **Bind 127.0.0.1 only.** Never `0.0.0.0`; never expose the port.
- **Scope tools tightly** — read-only papers ops, e.g.
  `--allowedTools 'Bash(python3 *papers.py search*)' 'Bash(python3 *papers.py show*)'
  'Bash(python3 *papers.py index*)' 'Bash(python3 *papers.py list*)' Read`.
  Do **not** grant arbitrary `Bash`, and do **not** use `--dangerously-skip-permissions`.
- Offer an explicit **read-only vs edit mode** toggle (edit mode additionally allows
  `add`/`gaps`/`bib`), with clear in-UI consent.
- Single-flight / rate-limit chat (each request spawns a process). Latency ~seconds/turn; `--resume`
  + `stream-json` keep it responsive.

## Milestones / effort
- **M1 — Browse MVP (no LLM):** server + `/api/{lists,search,paper}` + `/pdf/<id>`; UI sidebar +
  results + PDF viewer + markdown. ~1 day. Low risk; everything maps to existing CLI calls.
- **M2 — Chat tier:** `/api/chat` via `claude -p` stream-json over SSE; session resume; streaming
  markdown; clickable paper ids open PDFs. ~1–2 days.
- **M3 — Polish:** routing-index sidebar (domains/themes), open-paper tabs, keyboard nav, dark mode.
- **M4 — Annotations (later):** PDF.js annotation layer + a `notes` table in library.db. Long tail.

## Dependencies
- Backend: prefer **stdlib `http.server`** (zero deps, max portability) or FastAPI+uvicorn if handy.
- Frontend: `marked.js` + `DOMPurify` (vendored), `PDF.js` (optional, M4). No build step.
- Chat tier requires Claude Code installed + authenticated.

## Files to add
`web/server.py`, `web/static/index.html`, `web/static/app.js`, `web/static/style.css`,
`web/README.md`; plus a `papers serve` subcommand (or a `papers-web` launcher) in `papers.py`.

## Open design questions (decide in-project)
- stdlib `http.server` vs FastAPI (deps vs ergonomics).
- Session model: `--session-id` + `--resume`; how long to retain chat context.
- How the model signals "open paper N" to the UI (id-in-markdown convention vs a structured marker).
- Default mode: read-only vs edit.
- Packaging for group members (`pipx`, a launch script, or a tiny installer).

## Non-goals (v1)
Cloud hosting, multi-user auth, mobile, in-PDF annotation (M4+), citation-insertion word-processor
plugins.

## Policy update (2026-10-16): the chat may change what a button can change

The June decision below ("explore the corpus, never mutate the library") rested on two facts that
no longer hold: the only tool mechanism was scoped Bash, and nothing could be undone. The chat's
tools are now named functions in `web/chat_tools.py` (the model names a tool, the server decides),
the first write (`add_paper`, October) proved the gate, and list changes are snapshotted and
undoable. New policy: **the chat may do what the user could do with a button, when the user asks,
and every change can be undone; it never deletes a paper and never changes what a paper says.**
What stays protected: paper records (they come only from the authorities, through the admission
gate), the papers themselves, the user's deliberate steps (overrides, audits, syncs), and
everything outside the library. Each write is offered only on a turn whose own user message asks
for it, so text inside a paper cannot trigger one.

## Build decisions (2026-06-21)
Resolved the open questions during implementation:
- **Chat backend = Safe RAG, not a tool-wielding agent.** Per-decision, GUI model access is "explore
  the corpus, never mutate the library." So the server does the retrieval (routing index + ranked
  FTS) and hands it to `claude -p` with **all tools disallowed** — the model writes prose and cannot
  search the disk, run shells, fetch the network, or change the library. This is faster and cheaper
  than a per-turn agentic loop and removes the prompt-injection-via-Bash surface entirely. The
  "scoped-Bash agent" alternative was considered and rejected for v1 (glob Bash-scoping is a porous
  sandbox; abstracts are untrusted input). `add`/`gaps`/`bib` are unreachable from the browser.
- **Backend = stdlib `http.server`** (`ThreadingHTTPServer`), not FastAPI — the project is
  standard-library-only, and that keeps group packaging dependency-free. SSE for chat streaming.
- **Engine reuse via a query/print split.** `papers.py` commands printed text; added `query_search/
  show/lists/list/index` that *return* data (single SQL source of truth) and made the CLI printers
  call them. The web server imports `papers` and calls the same functions — no stdout parsing, no
  duplicated SQL. The CLI output is unchanged.
- **Security hardened beyond loopback bind.** Added a per-launch token + Host/Origin allowlist to
  defeat DNS-rebinding/CSRF (loopback bind alone does not). Chat is concurrency-capped.
- **PDF viewer = browser-native `<iframe>`**; markdown via a small escape-first safe renderer (no
  marked.js/DOMPurify dependency needed, no XSS surface). Chat retrieval ORs salient terms and ranks
  by FTS `rank` so a natural-language question still surfaces citable papers.
- **"Open paper N" signaling = `[id]` in markdown**, linkified by the UI to open `/pdf/<id>`.

Still open / future: M3 polish (keyboard nav, themes), M4 annotations (`notes` table + PDF.js),
packaging for group members (`pipx` / launcher), session-resume model if multi-turn context needs
to outgrow the server-side history window.
