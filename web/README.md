# papers-web — localhost GUI

A lightweight, **localhost-only** web front end over the papers-vela library: a sidebar of your
lists, full-text search, an in-browser PDF viewer with open-paper tabs, and a chat box that answers
questions over your library and cites clickable paper `[id]`s.

No build step, no framework, **standard library only** on the backend (matching the rest of the
project). Vanilla HTML/CSS/JS on the front end.

## Run

```bash
papers serve                 # starts the server and opens your browser
papers serve --port 9000     # custom port (default 8787; or set PAPERS_WEB_PORT)
papers serve --no-open       # don't auto-open a browser
```

It prints a URL with a one-time token:

```
papers-web → http://127.0.0.1:8787/?token=…
```

Open that exact URL (the token authorizes the session). `Ctrl-C` stops the server.

## Two tiers

**Browse (no LLM).** Pure reads over the same `query_*` functions the CLI uses, so the GUI and the
CLI never disagree:

| Endpoint | Backed by |
|---|---|
| `GET /api/lists` | `papers lists` |
| `GET /api/index?domain=&grep=` | `papers index` (routing view) |
| `GET /api/search?q=&list=&author=&year=&limit=` | `papers search` |
| `GET /api/paper/<id>` | `papers show` |
| `GET /pdf/<id>` | streams the file at the paper's `pdf_path` |
| `GET /api/comments/<id>` | the paper's comments (marks), `papers.query_comments` |
| `POST /api/comments/<id>[/update\|/delete]` | add / edit / delete a comment — owner only |
| `POST /api/lists/describe` `{name}` | the model rewrites a list's description from its papers (the chip) — owner only |
| `POST /api/lists/rename` `{name, new_name}` | rename a list — owner only |
| `POST /api/lists/merge` `{from, into}` | move `from`'s papers into `into`, then delete `from` — owner only |
| `POST /api/lists/delete` `{name}` | delete a list (papers stay) — owner only |
| `POST /api/lists/undo` `{id}` | undo one list change, from a button or from the chat — owner only |
| `POST /api/model` `{name}` | switch the chat to another of the profile's models (and make it the default) |

Every list change answers with `{label, undo}`; the toast's Undo (or the button under a chat reply)
sends the id back. Papers are never deleted or edited by any of these.

**PDF viewer and marks.** PDFs render in `static/pdfview.js`, a port of the session-dashboard
kit's viewer: pdf.js (vendored, `static/vendor/pdfjs/PROVENANCE.md`, no CDN) draws each page as a
canvas with a text layer over it, so text can be selected, copied and found with the browser's
⌘F. Fit-to-width / one-page / zoom chips; the page follows the pane as the dividers move.
Selecting a passage offers **✎ mark** with an optional comment; ▭ (or Alt-drag) marks a region —
a formula, table or figure. "✎ mark: off" lets a selection be copied without the offer (Enter
still marks). Marks are saved **with the paper** as comments in `library.db` — never into the PDF
— and listed under the paper's details, where they can be edited, deleted, or clicked to jump to
the highlight; "+ note" comments on the paper as a whole. A tab whose server was restarted says so
in a banner instead of failing silently.

**Chat.** `POST /api/chat {message, history}` →
1. the **server** retrieves context itself — the routing index plus rank-ordered full-text hits for
   the question (salient terms OR-ed, so a natural-language question still surfaces candidates);
2. it hands that context to the model the **profile** names, whose only tools are
   [`chat_tools.py`](chat_tools.py), thin wrappers over the engine:
   - **`claude`** (the user's Claude subscription): `claude -p` **isolated from the user's Claude
     Code setup**, with `chat_tools.py` as its only MCP server (stdio);
   - **`velella`** (tokens.velella.ca) or any **`openai`**-compatible endpoint: the server runs the
     tool loop itself (`run_openai`), calling the same functions in-process. The model can only
     *name* a tool; `call_tool` decides whether it runs. Enrolment is by key fingerprint
     (`papers init` makes the key, `papers profile` shows the fingerprint);
3. the model's prose is streamed back token-by-token over **SSE** and rendered as markdown, with
   `[id]` citations turned into buttons that open the PDF, and tool progress shown while it works.

The chat follows `/papers` in both halves. Its rules are the skill's own: the server reads the
RULES block of `skill/SKILL.md` (from this repo, so a backend without Claude Code gets them too)
into the chat's instructions on every turn, and switches the chat off if the block is missing. Its
tools are the skill's engine: it looks in the library first (`search_library`, `list_papers`,
`show_paper`, `read_paper`), goes to NASA ADS for anything not there (`search_ads`, journals and
arXiv/astro-ph), and adds a paper with `add_paper`, which runs `papers add` itself, so the admission
gate verifies the record and may refuse it, and `references.bib` is regenerated after. The
instructions in `server.py` (`LIBRARIAN`) add only how the rules apply in the GUI: tool names,
`[id]` buttons, the BibTeX link, the one-paper-per-yes add. If the configured backend is not
available (no `claude` CLI, or no key for an OpenAI-compatible endpoint), the chat tab is disabled
and browse still works.

## Security model

An LLM-adjacent HTTP port on your laptop deserves care. Three layers, none of which depends on the
others:

- **Loopback bind.** The server listens on `127.0.0.1` only — never `0.0.0.0`.
- **Per-launch token.** A random token is minted at startup and required on every `/api/*` and
  `/pdf/*` request (header `X-Papers-Token`, or `?token=` for the PDF `<iframe>`). It is compared
  with `secrets.compare_digest`. Static assets (HTML/CSS/JS) carry no data and are served freely.
- **Host / Origin allowlist.** Requests whose `Host` isn't `127.0.0.1`/`localhost` are refused, as
  are cross-origin `Origin`s. Loopback binding alone does **not** stop a malicious web page you're
  visiting from POSTing to `127.0.0.1` (DNS-rebinding / CSRF); this check does.
- **Isolated chat.** `claude -p --tools "" --strict-mcp-config --mcp-config <chat_tools only>
  --setting-sources local --disable-slash-commands`, run in an empty directory: no built-in tools,
  no MCP server but ours, no plugins, skills, hooks, user settings or CLAUDE.md. Naming what is
  *allowed* is the point. The earlier launch named built-ins to *deny*, and MCP tools from the
  user's plugins and connectors stayed live: the "read-only" chat was seen answering through a
  plugin's web search (2026-10-06). The chat may do what the user could do with a button, when the
  user asks, and every change can be undone; it never deletes a paper or changes what a paper says.
  Its writes are `add_paper` and the list tools (file papers into lists, rename, merge, delete,
  describe). Each kind is offered only when the library is writable **and** the user's own message
  asks for it (`ADD_INTENT`, `LIST_INTENT`) or says a short yes to the chat's own offer
  (`asks_to_add`, `asks_to_organise`, then ONE change), so a sentence inside an abstract or PDF
  cannot trigger a change. Each list change comes back with an Undo button under the reply.
  `add_paper` never passes `--force-unverified`; an override stays the user's own recorded CLI step. Titles, abstracts, PDF text and ADS results are untrusted data in
  the system prompt (prompt-injection guard). Chat requests are concurrency-capped (one or two at a
  time) since each spawns a process.

Model output is rendered with an escape-first markdown renderer (HTML is escaped before any
formatting is applied, and only a fixed set of safe tags is emitted), so model text can't inject
scripts.

## Configuration (env)

| Variable | Default | Meaning |
|---|---|---|
| `PAPERS_LIB` | `~/Papers` | library directory (db + `pdfs/`) — same as the CLI |
| `PAPERS_WEB_PORT` | `8787` | port to bind |
| `PAPERS_WEB_MODEL` | the chosen model's | at start, override the model id of the model in use |
| `PAPERS_CHAT_BACKEND` | profile, else `claude` | `claude`, `velella` or `openai` — at start, use the profile's first model of that backend instead of the default |
| `PAPERS_WEB_CHATLOG` | `1` | `0` stops logging chat turns to `~/.cache/papers-web/chatlog/chat-YYYY-MM-DD.jsonl` (message, server context, tool calls + results, answer, cost; local only) |
| `PAPERS_WEB_CHAT_CONCURRENCY` | `2` | max simultaneous chat processes |

## Files

- `server.py` — stdlib `ThreadingHTTPServer`; routing, security, PDF streaming, Safe-RAG chat.
- `static/index.html`, `static/app.js`, `static/style.css` — the single-page UI (no build).

## Not yet (deliberately)

In-PDF annotation / highlighting (would add a `notes` table + PDF.js), multi-user auth, cloud
hosting, mobile. v1 is a single-user, own-your-data, localhost tool.
