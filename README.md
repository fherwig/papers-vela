# papers-vela

**A vela for your research library.** A *vela* is a secure, governed space for AI-augmented work.
This one holds your papers: a local library of PDFs and metadata that you search, read, mark up
and grow, with an AI librarian that can only do what the library's own tools allow. It runs on your
laptop; your library stays on your disk.

What you get:

- **A web app** (`papers web`): your lists, search, a PDF viewer where you **mark passages and
  comment** on them, and a **chat** that searches your library and NASA ADS (journals and
  arXiv/astro-ph), summarises papers, and adds new ones when you ask.
- **Verified citations.** Every paper is checked against Crossref, NASA ADS or arXiv before it is
  admitted; a record that doesn't match its identifier is refused. `references.bib` is generated
  from the library, never typed by hand.
- **A library you own**: one SQLite file (`library.db`) plus a `pdfs/` folder. No subscription, no
  cloud account, no lock-in. A lightweight alternative to Mendeley, ReadCube or Zotero, and it can
  also sit on top of a Zotero library.
- **A command line** (`papers search`, `papers add`, …) for everything the app does, and a
  `/papers` skill that lets Claude Code use your library from any project.

## How it fits together

One library, one engine, and three ways in.

```
               your library: library.db + pdfs/
                               │
                     papers.py, the engine
        the only code that reads or writes the library
        ┌──────────────────────┬──────────────────────┐
  the terminal            Claude Code            the web app
    papers …           sessions, through   browse, read, mark up,
                       the /papers skill      and ask the chat
```

- **Your library** is your data: one SQLite file and a folder of PDFs.
- **The engine**, `papers.py`, is the only code that reads or writes the library. Everything else
  goes through it, so all three ways in see the same papers. A passage you mark in the web app is
  there for a Claude Code session, and a paper a session adds appears in the web app at once.
- **The web app** is where you look at papers, find them, read them, mark passages and ask about
  them. Its chat searches your library and NASA ADS, and adds papers when you ask. It runs on your
  Claude subscription or on an OpenAI-compatible model, so you can use it without Claude Code.
- **The `/papers` skill** is for Claude Code users. It is one file, `skill/SKILL.md`, that teaches
  any Claude Code session, in any project, to use your library: find papers while you write, add
  them through the citation check, regenerate `references.bib` and cite the keys. Install it in
  step 6 below.
- **The terminal**: the `papers` command does all of it by hand.

### One set of rules

The rules that keep citations right are written once, in the skill file: look in the library
first, then NASA ADS; never write a DOI, author list or BibTeX entry from memory; add papers only
through the citation check. Claude Code reads them from the skill, and the web app's chat reads the
same block, whichever model it runs on.

### What each one is for

| | The web app | `/papers` in Claude Code, or the terminal |
|---|---|---|
| Find, read and ask about papers | yes | yes |
| Add papers, through the citation check | yes, ask the chat | yes |
| Mark passages and comment on them | yes, in the PDF viewer | read them with `papers show` |
| Download PDFs | yes | yes |
| Organise lists: file papers, rename, delete, describe a list | only when a paper is added | yes |
| Generate `references.bib`, audit the library, check someone else's references, sync a copy | no | yes |

The web app is for reading and asking. The skill and the terminal are for organising the library
and for the writing side.

---

## Install

You need **Python 3.9 or newer** and **git**. Nothing else is installed: the code uses only
Python's standard library. Optional but recommended: **poppler** (`pdftotext`), so the chat can read
PDFs (`brew install poppler` on macOS, `apt install poppler-utils` on Debian/Ubuntu).

### 1. Get the code

```bash
git clone https://github.com/fherwig/papers-vela.git ~/papers-vela
```

### 2. Make the `papers` command

Add this line to your shell startup file (`~/.zshrc` on macOS, `~/.bashrc` on Linux), then open a
new terminal:

```bash
alias papers='python3 ~/papers-vela/papers.py'
```

### 3. Set up your profile

```bash
papers init
```

It asks four things and writes your answers to `~/.config/papers/profile.json`:

1. **Where your library lives**: a folder of your own, e.g. `~/Papers`. A new, empty library is
   created there if none exists.
2. **Your email** (optional): sent to Crossref and Unpaywall as a courtesy contact.
3. **A NASA ADS token**: free, and needed to search ADS. Get it at
   <https://ui.adsabs.harvard.edu/user/settings/token> (make an account, then copy the token) and
   paste it when asked. It is stored in `~/.config/ads/token`, readable only by you.
4. **Which model the chat uses**. Pick one (you can switch later by running `papers init` again):
   - **`claude`: your Claude subscription.** Install [Claude Code](https://claude.com/claude-code)
     and run `claude` once to log in with your Claude account. The chat then uses your own
     subscription.
   - **`velella`: tokens.velella.ca**, an open-weight model hosted for members of Falk Herwig's
     research group at the University of Victoria (currently Qwen 3.6). `papers init` creates a
     private key on your machine and shows you its **fingerprint**, a long string of letters and
     digits. **Send the fingerprint, not the key, to Falk Herwig** and ask to be enrolled. The
     chat works once he confirms. The key never leaves your laptop. If you lose it, it can't be
     recovered, only replaced: delete `~/.config/spigot/key`, run `papers init` again, and send
     the new fingerprint.

   Not in the group? Use `claude`, or point the chat at any OpenAI-compatible server that supports
   tool calls by editing the profile's `chat` entry:
   `{"backend": "openai", "url": "https://your-server/v1", "key_file": "~/.config/your-server/key"}`
   (`model` is optional; without it the first model the server lists is used).

Run `papers profile` any time to see your settings and your fingerprint.

### 4. Open the web app

```bash
papers web
```

This starts the app on your laptop and opens it in your browser. Run it again to reopen the
window; `papers web stop` shuts the app down. The app only listens on your own machine
(`127.0.0.1`), and each start creates a new access token, so after a restart an old browser tab
will tell you to reopen it.

### 5. Put papers in

- **Ask the chat**: *"find the new paper by Nandal on arXiv"*, then *"add it to my Pop III list"*.
- **By identifier**: `papers add 2609.21939` (arXiv), `papers add 10.3847/1538-4357/ae4487` (DOI),
  `papers add 2026ApJ..1000...89P` (ADS bibcode). Add `--list "My list"` to file it.
- **From another reference manager**: export your library as BibTeX (Zotero, Mendeley, ReadCube
  and EndNote all can), then `python3 ~/papers-vela/import_bib.py my_library.bib`.
- **From Zotero, kept in sync**: see the scripts in [`zotero/`](zotero/).

PDFs are fetched on demand: open a paper without one and click **Download PDF** (open-access copies
from the publisher, Unpaywall or arXiv), or file one you already have with
`papers attach <id> ~/Downloads/paper.pdf`.

### 6. Optional: the `/papers` skill for Claude Code

If you use Claude Code, link the skill so every session can use your library:

```bash
mkdir -p ~/.claude/skills/papers
ln -sf ~/papers-vela/skill/SKILL.md ~/.claude/skills/papers/SKILL.md   # a link, so it stays current
```

Then, in any Claude Code session: *"find my papers on convective boundary mixing"*, or *"add arXiv
2609.21939 to my Pop III list and cite it"*.

### Updating

```bash
cd ~/papers-vela && git pull && papers web stop && papers web
```

---

## Three places, kept apart

| | What | Where |
|---|---|---|
| **The engine** | this repository: code only, the same for everyone | `~/papers-vela` |
| **Your library** | your data: `library.db`, `pdfs/`, generated `references.bib` | the folder you chose, e.g. `~/Papers` |
| **Your profile** | your settings: library path, email, tokens, chat model | `~/.config/papers/profile.json` |

Nothing personal lives in the engine, and no code lives in the library. That is why the library can
be backed up, synced or shared without touching the code, and why updating the code never touches
your library. Environment variables override the profile for one-off runs (`PAPERS_LIB`,
`PAPERS_EMAIL`, `PAPERS_WEB_MODEL`, `PAPERS_CHAT_BACKEND`, `PAPERS_PROFILE`).

---

## The web app

Three panes, with dividers you can drag:

- **Left: your lists**, each with a one-line description. Click one to see its papers.
- **Middle: Search and Chat.**
  - **Search** understands `Herwig 2008` (an author's papers from a year), `Herwig 2008-2012`,
    `convective overshoot 2020` (a topic in a year) and plain topics (ranked full-text).
  - **Chat** answers in plain language over your library. It can point you to the right lists,
    summarise a paper (it reads the PDF), open a paper in the viewer, search NASA ADS for papers you
    don't have yet, and add a paper when you ask; a short "yes" to its offer to add a paper also
    counts. It never writes a DOI or a BibTeX entry from memory: identifiers come from the library
    or ADS, and a BibTeX request is pointed at the paper's BibTeX link. Every turn is logged on your
    machine (`~/.cache/papers-web/chatlog/`).
- **Right: the PDF viewer**, a tab per open paper. Above it: the title (click for authors, lists and
  the abstract) and one line of details, and the toolbar with **doi · arXiv · BibTeX · 💬 · + note**.
  **Select a passage** to mark it with a comment, or drag a **region** (▭, or hold Alt) around a
  formula, table or figure. Marks are saved **with the paper** in your library, listed under 💬,
  and printed by `papers show`. *✎ mark* switches the offer off so you can copy text instead.

**What the chat can and cannot do.** Its only tools are the library's own: search and read the
library, search NASA ADS, and add a paper through the same verified `papers add` the command line
uses. It cannot run commands, read your files, browse the web, or change anything else. It is
started isolated from any other Claude Code setup you have (no plugins, skills or other tools). An
add happens only on a turn where you asked for one, and a plain "yes" adds at most one paper. Full
design and security model: [`web/README.md`](web/README.md).

---

## The command line

Everything the app does is also a command:

```bash
papers search "Herwig 2008"                            # author + year, "topic 2007", or ranked text
papers show 123                                        # full record, lists, abstract, your comments
papers open 123                                        # open the PDF
papers lists                                           # all lists with counts
papers list "shell mergers"                            # the papers in a list
papers ads 'author:"^Herwig" year:2023-2026'          # search NASA ADS
papers add 2023MNRAS.525.1601H --list "shell mergers"  # add (verified first), file into a list
papers attach 123 ~/Downloads/paper.pdf                # file a PDF you already have
papers index --domain convection                       # which lists cover a topic
papers gaps --fetch-arxiv                              # fetch missing PDFs that are on arXiv
papers bib                                             # regenerate references.bib + INDEX.md
papers check-refs paper.bib                            # check someone else's references (read-only)
papers web | papers web stop | papers profile          # the app, and your settings
```

**Claude Code users:** the `/papers` skill (install step 6) lets Claude Code run these commands for
you.

---

## Troubleshooting

- **The chat tab is greyed out.** Hover over "chat: off" in the top bar for the reason: Claude Code
  not installed or not logged in, or no key for tokens.velella.ca (run `papers init`).
- **"tokens.velella.ca refused this key (401)".** Your fingerprint hasn't been enrolled yet, or you
  replaced the key. `papers profile` shows the fingerprint to send.
- **A red banner says the access token is no longer valid.** The app was restarted. Run `papers web`.
- **`papers` says it can't find a library.** Run `papers init`, or check `papers profile`.
- **Searching ADS says there is no token.** Run `papers init` and paste your ADS token.

---

## Layout

| Path | What |
|---|---|
| `papers.py` | the command line and engine |
| `web/` | the web app: a standard-library server, a no-build single page, the chat's tools (`chat_tools.py`), pdf.js (vendored) |
| `gate.py`, `verify.py`, `resolve.py`, `check_refs.py` | citation verification: admission gate, audit, source hierarchy, reference checking |
| `import_bib.py` | BibTeX → library |
| `generate_outputs.py`, `rebuild_bylist.py` | `references.bib`, `INDEX.md`, per-list pages |
| `zotero/` | keep a library in sync with Zotero |
| `trillium_sync.py` | sync a library with a copy on a remote cluster (the profile's `remote`) |
| `skill/SKILL.md` | the Claude Code `/papers` skill |
| `migration/` | scripts used to migrate off ReadCube/Papers 3 |

Contributors working on the code: start with [`CLAUDE.md`](CLAUDE.md).

Written by Falk Herwig and Joshua Issa.

## License

[GNU General Public License v3.0](LICENSE). Copyright 2026 Falk Herwig and Joshua Issa.

You may use, change and share papers-vela for any purpose, commercial use included. Anything you
distribute that is built on it must also be under the GPL, with its source code. The vendored pdf.js
(`web/static/vendor/pdfjs/`) keeps its own Apache-2.0 license, which is compatible with the GPL.
