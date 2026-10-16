"use strict";
// papers-web front end. Talks to the localhost server; all data calls carry the launch token.

// --- token: from the URL on first load (then cached per-tab so reloads keep working),
//     scrubbed from the address bar so it doesn't linger there ---
const TOKEN = new URLSearchParams(location.search).get("token") || sessionStorage.getItem("papersToken") || "";
if (TOKEN) sessionStorage.setItem("papersToken", TOKEN);
window.history.replaceState(null, "", location.pathname);

const $ = (s) => document.querySelector(s);
const el = (tag, cls, txt) => { const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };

// A 403 means this tab's token is stale: the server mints a new one each time it starts, so
// after a restart every request from an already-open tab is refused. That used to look like
// "the lists stopped working" (2026-10-06). A dead tab must say it is dead.
let tokenDead = false;
function tokenExpired() {
  if (tokenDead) return;
  tokenDead = true;
  const b = el("div", "token-dead");
  b.append(el("b", null, "This tab's access token is no longer valid — the papers server was restarted."),
           el("span", null, " Open the new address it printed (or run: webapp papers)."));
  document.body.append(b);
}
async function api(path, opts) {
  const o = opts || {};
  const r = await fetch(path, { method: o.method || "GET",
    headers: { "X-Papers-Token": TOKEN, ...(o.body ? { "Content-Type": "application/json" } : {}) },
    body: o.body ? JSON.stringify(o.body) : undefined });
  if (r.status === 403 && /token/i.test(r.statusText + (await r.clone().text().catch(() => "")))) tokenExpired();
  if (!r.ok) {
    let msg = `${path} → ${r.status}`;
    try { const j = await r.json(); if (j && j.error) msg = j.error; } catch (_e) {}
    throw new Error(msg);
  }
  return r.json();
}
const pdfURL = (id) => `/pdf/${id}?token=${encodeURIComponent(TOKEN)}`;
let _toastT = null;
function toast(msg, action) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  if (action) {
    const b = el("button", "tbtn", action.label);
    b.onclick = () => { t.hidden = true; action.run(); };
    t.append(b);
  }
  clearTimeout(_toastT); _toastT = setTimeout(() => { t.hidden = true; }, action ? 7000 : 2600);
}

// Copy to the clipboard (127.0.0.1 is a secure context, so the Clipboard API is available). If the
// browser refuses, select the text instead so ⌘C takes it: never a pop-up.
function copyText(text, done, node) {
  const fallback = () => {
    if (node) { const r = document.createRange(); r.selectNodeContents(node);
                const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r); }
    toast(`Press ⌘C to copy ${text}`);
  };
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(() => toast(done), fallback);
  else fallback();
}

// NO BROWSER POP-UPS (Falk, 2026-10-09: "I never want to see browser pop up windows as input").
// Text is entered inline: Enter saves, Shift+Enter is a new line, Esc cancels. Destructive actions
// act at once and offer Undo in the toast instead of an "are you sure?" box.
function inlineEditor({ initial, placeholder, saveLabel, onSave, onCancel }) {
  const box = el("div", "ied");
  const ta = el("textarea"); ta.rows = 2; ta.value = initial || ""; ta.placeholder = placeholder || "";
  const save = el("button", "pbtn", saveLabel || "Save"), cancel = el("button", "pbtn", "Cancel");
  const grow = () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 180) + "px"; };
  const go = async () => {
    const v = ta.value.trim();
    if (!v) { ta.focus(); return; }
    save.disabled = true;
    try { await onSave(v); } catch (err) { toast("not saved — " + err.message); save.disabled = false; }
  };
  ta.oninput = grow;
  ta.onkeydown = (e) => {
    e.stopPropagation();
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); go(); }
    if (e.key === "Escape") { e.preventDefault(); onCancel(); }
  };
  save.onclick = go; cancel.onclick = onCancel;
  box.append(ta, el("div", "ied-btns"));
  box.lastChild.append(el("span", "muted", "Enter saves · Shift+Enter new line · Esc cancels"), cancel, save);
  // focused by the caller the moment it is in the page, not a tick later: a tick later, the
  // first keystrokes after the click landed in the search box (headless test, 2026-10-09)
  box._focus = () => { grow(); ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); };
  return box;
}

// ===================== safe markdown (escape-first, whitelisted tags) =====================
function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
// inline formatting on already-escaped text; emits only known-safe tags
function inline(s) {
  s = s.replace(/`([^`]+)`/g, (_m, c) => `<code>${c}</code>`);
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
        (_m, t, u) => `<a href="${u}" target="_blank" rel="noopener noreferrer">${t}</a>`);
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/__([^_]+)__/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*])\*([^*\s][^*]*?)\*/g, "$1<em>$2</em>");
  // citation [123] -> clickable, but not when it's actually a markdown link [..](..)
  s = s.replace(/\[(\d{1,6})\](?!\()/g, (_m, id) => `<a class="cite" data-id="${id}">[${id}]</a>`);
  // suggested actions -> clickable chips that run in the GUI (no terminal needed)
  s = s.replace(/papers\s+search\s+(?:&quot;|&#39;|["'])([^"'&]+?)(?:&quot;|&#39;|["'])/gi,
        (_m, q) => `<a class="act" data-act="search" data-arg="${q}">\u{1F50D} ${q}</a>`);
  s = s.replace(/papers\s+list\s+(?:&quot;|&#39;|["'])([^"'&]+?)(?:&quot;|&#39;|["'])/gi,
        (_m, q) => `<a class="act" data-act="list" data-arg="${q}">\u{1F4C2} ${q}</a>`);
  s = s.replace(/\bpapers\s+(?:open|show)\s+(\d{1,6})\b/gi,
        (_m, id) => `<a class="cite" data-id="${id}">open [${id}]</a>`);
  return s;
}
const splitRow = (line) => line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((x) => x.trim());
const isTableSep = (line) => /^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)+\|?\s*$/.test(line || "");

function renderMd(src) {
  const lines = src.replace(/\r/g, "").split("\n");
  let html = "", i = 0, ul = false, ol = false;
  const close = () => { if (ul) { html += "</ul>"; ul = false; } if (ol) { html += "</ol>"; ol = false; } };
  while (i < lines.length) {
    const raw = lines[i];
    if (/^\s*```/.test(raw)) {                         // fenced code
      close(); i++; let code = "";
      while (i < lines.length && !/^\s*```/.test(lines[i])) { code += lines[i] + "\n"; i++; }
      i++; html += `<pre><code>${escapeHtml(code)}</code></pre>`; continue;
    }
    if (raw.includes("|") && isTableSep(lines[i + 1])) {  // GFM pipe table
      close();
      const header = splitRow(raw); i += 2;
      let body = "";
      while (i < lines.length && lines[i].includes("|") && lines[i].trim() !== "") {
        const cells = splitRow(lines[i]);
        body += "<tr>" + header.map((_, j) => `<td>${inline(escapeHtml(cells[j] || ""))}</td>`).join("") + "</tr>";
        i++;
      }
      const head = "<tr>" + header.map((h) => `<th>${inline(escapeHtml(h))}</th>`).join("") + "</tr>";
      html += `<table><thead>${head}</thead><tbody>${body}</tbody></table>`;
      continue;
    }
    const esc = escapeHtml(raw);
    let m;
    if ((m = esc.match(/^(#{1,6})\s+(.*)$/))) { close(); const lv = Math.min(m[1].length + 2, 6); html += `<h${lv}>${inline(m[2])}</h${lv}>`; i++; continue; }
    if ((m = esc.match(/^\s*[-*]\s+(.*)$/))) { if (ol) { html += "</ol>"; ol = false; } if (!ul) { html += "<ul>"; ul = true; } html += `<li>${inline(m[1])}</li>`; i++; continue; }
    if ((m = esc.match(/^\s*\d+\.\s+(.*)$/))) { if (ul) { html += "</ul>"; ul = false; } if (!ol) { html += "<ol>"; ol = true; } html += `<li>${inline(m[1])}</li>`; i++; continue; }
    if (esc.trim() === "") { close(); i++; continue; }
    close(); let para = esc; i++;                       // paragraph: gather until a block break
    while (i < lines.length && lines[i].trim() !== "" && !/^\s*(```|[-*]\s|\d+\.\s|#{1,6}\s)/.test(lines[i])) {
      para += " " + escapeHtml(lines[i]); i++;
    }
    html += `<p>${inline(para)}</p>`;
  }
  close();
  return html;
}

// ===================== state =====================
let LISTS = [], INDEX = {};           // INDEX: name -> {blurb, domains, status, ...}
let CONFIG = {};                      // /api/config: {lib, chat, model, writable}
const tabs = [];                      // open paper tabs: {id, title, d}
let activeTab = null;

// ===================== lists sidebar =====================
function renderLists(filter) {
  const box = $("#lists"); box.textContent = "";
  const f = (filter || "").toLowerCase();
  for (const l of LISTS) {
    if (f && !l.name.toLowerCase().includes(f)) continue;
    const meta = INDEX[l.name] || {};
    const row = el("div", "listrow");
    const head = el("div");
    head.appendChild(el("span", "ln", l.name));
    head.appendChild(el("span", "lc", `${l.n}p · ${l.pdf}pdf`));
    row.appendChild(head);
    if (meta.blurb) row.appendChild(el("div", "lb", meta.blurb));
    if (meta.domains) for (const d of meta.domains.split(",").map((s) => s.trim()).filter(Boolean).slice(0, 4)) row.appendChild(el("span", "chip", d));
    if (meta.status === "stale") { const s = el("span", "chip stale", "⚠ blurb stale"); row.appendChild(s); }
    row.onclick = () => loadList(l.name);
    box.appendChild(row);
  }
}

async function loadList(name) {
  switchPane("search");
  $("#q").value = "";
  $("#resulthead").textContent = `list: ${name}`;
  try { renderResults(await api(`/api/search?list=${encodeURIComponent(name)}&limit=300`)); }
  catch (e) { $("#results").textContent = String(e); }
}

// ===================== search =====================
function renderResults(rows) {
  const box = $("#results"); box.textContent = "";
  if (!rows.length) { box.appendChild(el("div", "muted", "no matches.")); return; }
  for (const p of rows) {
    const r = el("div", "res");
    const t = el("div", "t"); t.textContent = p.title || "(untitled)"; r.appendChild(t);
    const m = el("div", "m");
    const flag = el("span", p.has_pdf ? "pdfflag" : "nopdf", p.has_pdf ? "✓ " : "○ ");
    m.appendChild(flag);
    m.appendChild(document.createTextNode(`${p.year || "????"} · ${p.author} · [${p.id}]${p.journal ? " · " + p.journal : ""}`));
    if (p.verif && p.verif.state !== "verified") {
      const w = el("span", "vwarn", ` ⚠ ${p.verif.label}`); w.title = p.verif.detail || ""; m.appendChild(w);
    }
    r.appendChild(m);
    r.onclick = () => openPaper(p.id);
    box.appendChild(r);
  }
}

async function runSearchQuery(q, prefix) {
  $("#resulthead").textContent = q ? `search: ${q}` : "";
  if (!q) { $("#results").textContent = ""; return; }
  const url = `/api/search?q=${encodeURIComponent(q)}&limit=100` + (prefix ? "&prefix=1" : "");
  try { renderResults(await api(url)); }
  catch (err) { $("#results").textContent = String(err); }
}

$("#searchform").addEventListener("submit", async (e) => {
  e.preventDefault();
  runSearchQuery($("#q").value.trim(), false);
});

let _searchTimer = null;
$("#q").addEventListener("input", (e) => {
  clearTimeout(_searchTimer);
  const q = e.target.value.trim();
  if (!q) { $("#results").textContent = ""; $("#resulthead").textContent = ""; return; }
  // Don't fire live search until there's at least one non-trivial token (≥3 chars, not all hyphens/spaces)
  _searchTimer = setTimeout(() => runSearchQuery(q, true), 250);
});

// ===================== paper tabs + PDF viewer =====================
async function openPaper(id) {
  id = Number(id);
  let d = null;
  try { d = await api(`/api/paper/${id}`); } catch (_e) {}
  let tab = tabs.find((t) => t.id === id);
  if (tab) { if (d) tab.d = d; } else { tab = { id, title: (d && d.title) || `#${id}`, d }; tabs.push(tab); }
  activeTab = id; tab.used = ++tabClock;
  if (d) showDetail(d);
  else { $("#detail").className = "muted"; $("#detail").textContent = `No paper with id ${id} in this library.`; }
  showViewer(id, tab.d);
  renderTabs();
}

// The PDF in the kit's viewer (pdfview.js: text layer, marks), or a friendly panel when there
// is no local PDF. One viewer per open tab, hidden when not active, so each keeps its place.
const viewers = new Map();            // paper id -> PdfView
function showViewer(id, d) {
  const m = $("#viewermsg");
  for (const [vid, v] of viewers) v.el.style.display = vid === id ? "" : "none";
  if (d && d.has_pdf) {
    m.style.display = "none"; $("#pdfhost").style.display = "";
    if (!viewers.has(id)) {
      viewers.set(id, PdfView.mount($("#pdfhost"), {
        url: pdfURL(id), canMark: !!CONFIG.writable, toast,
        onMark: async (rec) => { await api(`/api/comments/${id}`, { method: "POST", body: rec });
                                 toast("marked" + (rec.note ? " with a comment" : "")); await loadComments(id); },
        onPin: (cid) => openComments(id, cid),
      }));
      loadComments(id);
    }
    placeActions(id);
  } else {
    $("#pdfhost").style.display = "none";
    m.style.display = "flex"; m.innerHTML = noPdfHtml(id, d);
  }
}

// ===================== comments: the reader's marks, saved with the paper =====================
async function loadComments(id) {
  const t = tabs.find((x) => x.id === id);
  if (!t) return;
  try { t.comments = (await api(`/api/comments/${id}`)).items || []; } catch (_e) { t.comments = t.comments || []; }
  const v = viewers.get(id); if (v) v.setMarks(t.comments);
  if (activeTab === id) renderComments(t);
}
function openComments(id, cid) {
  const t = tabs.find((x) => x.id === id); if (!t) return;
  t.commentsOpen = true; renderComments(t);
  const row = document.querySelector(`#comments .crow[data-cid="${cid}"]`);
  if (row) { row.classList.add("on"); row.scrollIntoView({ block: "nearest" }); }
}
function renderComments(t) {
  const box = $("#comments"); if (!box) return;
  box.textContent = "";
  const cs = t.comments || [];
  const tog = $("#ctog");
  if (tog) {
    tog.textContent = cs.length ? `💬 ${cs.length}${t.commentsOpen ? " ▴" : ""}` : "💬 0";
    tog.title = cs.length ? `${cs.length} comment${cs.length === 1 ? "" : "s"} on this paper — click to ${t.commentsOpen ? "hide" : "show"}`
                          : "No comments yet. Select a passage in the PDF to mark it, or use + note.";
    tog.classList.toggle("has", cs.length > 0);
  }
  if (t.noteDraft) {                                   // "+ note": a comment on the whole paper
    box.append(inlineEditor({
      placeholder: "Comment on this paper (to comment on a passage, select it in the PDF)",
      saveLabel: "Add note",
      onSave: async (note) => {
        await api(`/api/comments/${t.id}`, { method: "POST", body: { note, kind: "note" } });
        t.noteDraft = false; t.commentsOpen = true; await loadComments(t.id);
      },
      onCancel: () => { t.noteDraft = false; renderComments(t); },
    }));
  }
  if (!t.commentsOpen || !cs.length) { focusEditor(box); return; }
  const list = el("div", "clist");
  for (const c of cs) {
    const row = el("div", "crow"); row.dataset.cid = c.id;
    row.append(el("span", "cpg", c.page ? `p.${c.page}` : "paper"));
    const body = el("span", "cbody");
    if (c.quote) body.append(el("span", "cq", `“${c.quote.length > 140 ? c.quote.slice(0, 139) + "…" : c.quote}”`));
    if (t.editing === c.id) {
      body.append(inlineEditor({
        initial: c.note, placeholder: "Comment",
        onSave: async (note) => {
          await api(`/api/comments/${t.id}/update`, { method: "POST", body: { cid: c.id, note } });
          t.editing = null; await loadComments(t.id);
        },
        onCancel: () => { t.editing = null; renderComments(t); },
      }));
      body.onclick = (e) => e.stopPropagation();       // typing in the box must not jump the PDF
    } else if (c.note) body.append(el("span", "cn", c.note));
    row.append(body);
    if (CONFIG.writable && t.editing !== c.id) {
      const ed = el("span", "cact", "✎"); ed.title = c.note ? "Edit the comment" : "Add a comment to this mark";
      ed.onclick = (e) => { e.stopPropagation(); t.editing = c.id; t.noteDraft = false; renderComments(t); };
      const del = el("span", "cact", "✕"); del.title = "Delete this comment (Undo is offered)";
      del.onclick = async (e) => {
        e.stopPropagation();
        try {
          await api(`/api/comments/${t.id}/delete`, { method: "POST", body: { cid: c.id } });
          await loadComments(t.id);
          toast("comment deleted", { label: "Undo", run: async () => {
            try { await api(`/api/comments/${t.id}`, { method: "POST",
                    body: { page: c.page, quote: c.quote, note: c.note, rects: c.rects, kind: c.kind } });
                  t.commentsOpen = true; await loadComments(t.id); toast("comment restored"); }
            catch (err) { toast("not restored — " + err.message); }
          } });
        } catch (err) { toast("not deleted — " + err.message); }
      };
      row.append(ed, del);
    }
    row.onclick = () => { const v = viewers.get(t.id); if (v && c.page) v.focus(c.id); };
    list.append(row);
  }
  box.append(list);
  focusEditor(box);
}
function focusEditor(box) { const ed = box.querySelector(".ied"); if (ed && ed._focus) ed._focus(); }

function noPdfHtml(id, d) {
  const ax = d && d.arxiv, doi = d && d.doi, links = [];
  if (ax) links.push(`<a href="https://arxiv.org/abs/${encodeURIComponent(ax)}" target="_blank" rel="noopener">Read on arXiv ↗</a>`);
  if (doi) links.push(`<a href="https://doi.org/${encodeURIComponent(doi)}" target="_blank" rel="noopener">View at publisher ↗</a>`);
  if (ax || doi) links.push(`<a class="np-dl" data-id="${id}" href="#">Download PDF ⤓</a>`);
  const read = links.length ? `<div class="np-links">${links.join(" &nbsp;·&nbsp; ")}</div>` : "";
  const note = (ax || doi) ? "" : `<div class="muted">No open-access source on record for this paper.</div>`;
  return `<div class="np-ic">📄</div><div class="np-h">No local PDF yet</div>${read}${note}<div class="np-status muted"></div>`;
}

// "Download PDF" in the no-PDF panel — owner-only write; on a read-only library it explains why not
$("#viewermsg").addEventListener("click", async (e) => {
  const a = e.target.closest(".np-dl");
  if (!a) return;
  e.preventDefault();
  const id = Number(a.dataset.id), status = $("#viewermsg .np-status");
  status.textContent = "downloading…"; a.style.pointerEvents = "none"; a.style.opacity = ".5";
  try {
    const r = await fetch(`/api/fetch/${id}`, { method: "POST", headers: { "X-Papers-Token": TOKEN } });
    const j = await r.json();
    if (j.ok) {
      const t = tabs.find((t) => t.id === id); if (t && t.d) t.d.has_pdf = true;
      showViewer(id, (t && t.d) || { has_pdf: true });          // swap straight to the fetched PDF
    } else { status.textContent = j.message || "could not download"; a.style.pointerEvents = ""; a.style.opacity = ""; }
  } catch (err) { status.textContent = String(err); a.style.pointerEvents = ""; a.style.opacity = ""; }
});

// More than a few papers side by side is unreadable, so at most MAX_TABS stay tabs (fewer when the
// pane is narrow): the ones used most recently. The rest wait in the "▾ N more" menu, which lists
// every open paper. An in-page menu, never a browser pop-up.
const MAX_TABS = 4;
let tabClock = 0;                       // recency of use, so the papers you are reading stay tabs

function activateTab(t) {
  t.used = ++tabClock; activeTab = t.id;
  if (t.d) showDetail(t.d);
  showViewer(t.id, t.d); renderTabs();
}

function renderTabs() {
  closeTabMenu();
  const box = $("#papertabs");
  const recent = [...tabs].sort((a, b) => (b.used || 0) - (a.used || 0));
  for (let n = Math.min(MAX_TABS, tabs.length); ; n--) {
    const shown = new Set(recent.slice(0, n).map((t) => t.id));
    drawTabs(box, tabs.filter((t) => shown.has(t.id)), tabs.length - shown.size);
    if (n <= 1 || box.scrollWidth <= box.clientWidth) break;
  }
}

function drawTabs(box, shown, hidden) {
  box.textContent = "";
  for (const t of shown) {
    const tab = el("div", "ptab" + (t.id === activeTab ? " active" : ""));
    const lbl = el("span", "lbl", t.title); lbl.title = t.title;
    lbl.onclick = () => activateTab(t);
    tab.appendChild(lbl);
    const x = el("span", "x", "✕");
    x.onclick = (ev) => { ev.stopPropagation(); closeTab(t.id); };
    tab.appendChild(x);
    tab.onauxclick = (ev) => { if (ev.button === 1) { ev.preventDefault(); closeTab(t.id); } };
    box.appendChild(tab);
  }
  if (hidden > 0) {
    const m = el("div", "ptab more", `▾ ${hidden} more`);
    m.title = "all open papers";
    m.onclick = (ev) => { ev.stopPropagation(); if ($("#tabmenu")) closeTabMenu(); else openTabMenu(m); };
    box.appendChild(m);
  }
}

function openTabMenu(btn) {
  const m = el("div"); m.id = "tabmenu";
  for (const t of [...tabs].sort((a, b) => (b.used || 0) - (a.used || 0))) {
    const d = t.d || {};
    const row = el("div", "mi" + (t.id === activeTab ? " active" : ""));
    const who = [d.authors ? shortAuthors(d.authors).split(",")[0].replace(/ et al\.?$/, "") : "", d.year].filter(Boolean).join(" ");
    row.append(el("span", "num", String(t.id)), el("span", "au", who), el("span", "ti", t.title));
    row.title = t.title;
    const x = el("span", "x", "✕"); x.title = "close this paper";
    x.onclick = (ev) => {                 // close, and keep the menu open for the next one
      ev.stopPropagation(); closeTab(t.id);
      const b = document.querySelector("#papertabs .more"); if (b) openTabMenu(b);
    };
    row.append(x);
    row.onclick = () => { closeTabMenu(); activateTab(t); };
    row.onauxclick = (ev) => { if (ev.button === 1) { ev.preventDefault(); x.onclick(ev); } };
    m.append(row);
  }
  document.body.append(m);
  const r = btn.getBoundingClientRect();
  m.style.top = `${r.bottom + 2}px`;
  m.style.left = `${Math.max(8, Math.min(r.left, innerWidth - m.offsetWidth - 8))}px`;
  document.addEventListener("mousedown", tabMenuOutside, true);
  document.addEventListener("keydown", tabMenuKey, true);
}

function closeTabMenu() {
  const m = $("#tabmenu"); if (m) m.remove();
  document.removeEventListener("mousedown", tabMenuOutside, true);
  document.removeEventListener("keydown", tabMenuKey, true);
}
function tabMenuOutside(ev) {
  if (!ev.target.closest("#tabmenu, #papertabs .more")) closeTabMenu();
}
function tabMenuKey(ev) { if (ev.key === "Escape") { ev.stopPropagation(); closeTabMenu(); } }

let _tabsW = 0;                         // refit the tabs when the pane is resized
new ResizeObserver(() => {
  const w = $("#papertabs").clientWidth;
  if (w !== _tabsW) { _tabsW = w; renderTabs(); }
}).observe($("#papertabs"));

function closeTab(id) {
  const i = tabs.findIndex((t) => t.id === id);
  if (i < 0) return;
  tabs.splice(i, 1);
  const v = viewers.get(id); if (v) { v.destroy(); viewers.delete(id); }
  if (activeTab === id) {
    const next = [...tabs].sort((a, b) => (b.used || 0) - (a.used || 0))[0];   // the one read last
    if (next) { activeTab = next.id; if (next.d) showDetail(next.d); showViewer(next.id, next.d); }
    else { activeTab = null;
           $("#viewermsg").style.display = "none"; $("#detail").textContent = "Open a paper to view its PDF here."; $("#detail").className = "muted"; }
  }
  renderTabs();
}

// "Pathak, P. and Blouin, S. and …" -> "Pathak, Blouin, Herwig et al."
function shortAuthors(a) {
  const names = (a || "").split(/\s+and\s+/).map((x) => x.trim()).filter(Boolean);
  const sur = names.map((n) => (n.includes(",") ? n.split(",")[0] : n.split(/\s+/).pop()).trim());
  return sur.length > 3 ? sur.slice(0, 3).join(", ") + " et al." : sur.join(", ");
}
// The usual short forms, so the header's meta line fits; the full name stays in the tooltip.
const JOURNAL_ABBR = {
  "the astrophysical journal": "ApJ", "astrophysical journal": "ApJ",
  "the astrophysical journal letters": "ApJL", "astrophysical journal letters": "ApJL",
  "the astrophysical journal supplement series": "ApJS", "astrophysical journal supplement series": "ApJS",
  "the astronomical journal": "AJ", "monthly notices of the royal astronomical society": "MNRAS",
  "astronomy & astrophysics": "A&A", "astronomy and astrophysics": "A&A",
  "annual review of astronomy and astrophysics": "ARA&A", "physical review letters": "PRL",
  "physical review c": "PRC", "physical review d": "PRD", "physical review e": "PRE",
  "nature astronomy": "Nat. Astron.", "nature physics": "Nat. Phys.",
  "publications of the astronomical society of the pacific": "PASP",
  "publications of the astronomical society of australia": "PASA",
  "the european physical journal a": "EPJ A", "nuclear physics a": "Nucl. Phys. A",
  "journal of physics g: nuclear and particle physics": "J. Phys. G",
  "research notes of the aas": "RNAAS", "the open journal of astrophysics": "OJAp",
  "open journal of astrophysics": "OJAp", "arxiv e-prints": "arXiv",
};
const abbrJournal = (j) => JOURNAL_ABBR[(j || "").trim().toLowerCase()] || j || "";
const link = (txt, href, title) => {
  const a = el("a", null, txt); a.href = href || "#"; if (title) a.title = title;
  if (href) { a.target = "_blank"; a.rel = "noopener"; }
  return a;
};

// Paper header, two lines above the viewer: the title (one line; click it for full authors,
// lists and the abstract), then one line of meta, status and actions that wraps only when the
// pane is too narrow for it.
function showDetail(d) {
  const box = $("#detail"); box.className = ""; box.textContent = "";
  document.querySelectorAll(".d-acts").forEach((n) => n.remove());   // a hidden tab's toolbar copy
  const t = tabs.find((x) => x.id === d.id) || {};
  box.classList.toggle("more", !!t.more);
  const toggleMore = () => { t.more = !t.more; showDetail(d); };

  const title = el("div", "d-title", d.title || `#${d.id}`);
  title.title = t.more ? "click to collapse" : (d.title || "") + "\n\nclick for authors, lists and abstract";
  title.onclick = toggleMore;
  box.appendChild(title);

  const row = el("div", "d-row");
  const add = (node) => { if (row.children.length) row.appendChild(el("span", "sep", "·")); row.appendChild(node); };
  const idc = el("span", "d-id"), num = el("span", "num", String(d.id));   // the paper ID: click copies it
  idc.append(el("span", "lbl", "ID"), num);
  idc.title = `paper ID ${d.id}: click to copy (papers show ${d.id}, or [${d.id}] in the chat)`;
  idc.onclick = () => copyText(String(d.id), `Copied paper ID ${d.id}`, num);
  row.appendChild(idc);
  const au = el("span", "d-au", shortAuthors(d.authors)); au.title = d.authors || ""; add(au);
  if (d.year) add(el("span", null, String(d.year)));
  const jr = [abbrJournal(d.journal), d.volume, d.pages].filter(Boolean).join(" ");
  if (jr) { const j = el("span", "d-jr", jr); j.title = [d.journal, d.volume, d.pages].filter(Boolean).join(" "); add(j); }
  if (d.verif) {
    const ok = d.verif.state === "verified";
    const v = el("span", ok ? "vok" : "vwarn", ok ? "✓ verified" : "⚠ " + d.verif.label);
    v.title = d.verif.label + (d.verif.detail ? "\n" + d.verif.detail : ""); add(v);
  }
  if (d.lists && d.lists.length) {
    const ls = el("span", "d-lists", "📂 " + (d.lists.length > 2 ? d.lists.length + " lists" : d.lists.join(", ")));
    ls.title = d.lists.join("\n"); ls.onclick = () => (d.lists.length === 1 ? loadList(d.lists[0]) : toggleMore());
    add(ls);
  }
  const acts = el("span", "d-acts");
  if (d.doi) acts.append(link("doi", "https://doi.org/" + d.doi, d.doi));
  if (d.arxiv) acts.append(link("arXiv", "https://arxiv.org/abs/" + d.arxiv, "arXiv:" + d.arxiv));
  const bib = link("BibTeX"); bib.onclick = (e) => { e.preventDefault(); showBibtex(d.id); }; acts.append(bib);
  const ct = link("💬"); ct.id = "ctog";
  ct.onclick = (e) => { e.preventDefault(); t.commentsOpen = !t.commentsOpen; renderComments(t); };
  acts.append(ct);
  if (CONFIG.writable) {
    const nn = link("+ note", null, "Comment on the paper as a whole. To comment on a passage, select it in the PDF.");
    nn.onclick = (e) => { e.preventDefault(); t.noteDraft = !t.noteDraft; t.editing = null; renderComments(t); };
    acts.append(nn);
  }
  const mo = link(t.more ? "▴" : "▾", null, t.more ? "less" : "authors, lists, abstract");
  mo.onclick = (e) => { e.preventDefault(); toggleMore(); }; acts.append(mo);
  row.appendChild(acts);                 // moved into the PDF toolbar when there is one (placeActions)
  box.appendChild(row);

  if (t.more) {
    const more = el("div", "d-more");
    more.append(el("div", "d-full", d.authors || ""));
    if (d.lists && d.lists.length) {
      const lz = el("div", "d-full");
      lz.append("lists: ");
      d.lists.forEach((n, i) => { const a = link(n); a.onclick = (e) => { e.preventDefault(); loadList(n); };
                                 if (i) lz.append(", "); lz.append(a); });
      more.append(lz);
    }
    if (d.verif && d.verif.detail) more.append(el("div", "d-full muted", d.verif.label + " — " + d.verif.detail));
    if (d.abstract) more.append(el("div", "d-abs", d.abstract));
    box.appendChild(more);
  }
  const cm = el("div"); cm.id = "comments"; box.appendChild(cm);
  if (t.id) { if (t.comments) renderComments(t); else loadComments(d.id); }
  placeActions(d.id);
}

// With a PDF open, the paper's actions (doi · arXiv · BibTeX · 💬 · + note · ▾) sit at the right
// end of the viewer's toolbar, which is otherwise empty there; without one they stay in the
// header's meta line. Only one copy exists, so #ctog stays unique.
function placeActions(id) {
  const acts = $("#detail .d-acts"), v = viewers.get(id);
  if (acts && v && v.el.style.display !== "none") v.slot.replaceChildren(acts);
}

// ===================== BibTeX popup =====================
async function showBibtex(id) {
  const pre = $("#bibtext"); pre.textContent = "loading…";
  $("#bibmodal").hidden = false;
  try { const d = await api(`/api/bib/${id}`); pre.textContent = d.bibtex || "(unavailable)"; }
  catch (e) { pre.textContent = String(e); }
}
$("#bibclose").onclick = () => { $("#bibmodal").hidden = true; };
$("#bibmodal").addEventListener("click", (e) => { if (e.target.id === "bibmodal") $("#bibmodal").hidden = true; });
$("#bibcopy").onclick = async () => {
  try {
    await navigator.clipboard.writeText($("#bibtext").textContent);
    $("#bibcopy").textContent = "Copied ✓"; setTimeout(() => ($("#bibcopy").textContent = "Copy"), 1200);
  } catch (_e) { /* clipboard blocked — user can select the text manually */ }
};
document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#bibmodal").hidden = true; });

// ===================== chat (SSE over Safe RAG) =====================
const chatHistory = [];               // NB: must not be named `history` — that shadows window.history
// groups this tab's turns in the server's chat log (~/.cache/papers-web/chatlog/)
const CONV = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now())).slice(0, 8);
let chatBusy = false;

function addMsg(role, text) {
  const m = el("div", "msg " + role);
  if (role === "bot") m.innerHTML = text ? renderMd(text) : "";
  else m.textContent = text;
  $("#transcript").appendChild(m);
  $("#transcript").scrollTop = $("#transcript").scrollHeight;
  return m;
}

// delegate clicks on [id] citations and action chips
$("#transcript").addEventListener("click", (e) => {
  const c = e.target.closest(".cite");
  if (c && c.dataset.id) { openPaper(c.dataset.id); return; }
  const a = e.target.closest(".act");
  if (!a) return;
  if (a.dataset.act === "search") runSearch(a.dataset.arg);
  else if (a.dataset.act === "list") loadList(a.dataset.arg);
});

function runSearch(q) {
  switchPane("search");
  $("#q").value = q;
  $("#searchform").requestSubmit();
}

async function sendChat(message) {
  if (chatBusy || !message) return;
  chatBusy = true; $("#chatsend").disabled = true;
  addMsg("user", message);
  chatHistory.push({ role: "user", text: message });
  const bubble = addMsg("bot", ""); bubble.classList.add("typing");
  let raw = "";
  const status = (txt) => {                            // tool progress, shown until text arrives
    if (!raw) bubble.innerHTML = `<span class='muted'>${escapeHtml(txt)}</span>`;
  };
  try {
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "X-Papers-Token": TOKEN, "Content-Type": "application/json" },
      body: JSON.stringify({ message, history: chatHistory.slice(0, -1), conv: CONV }),
    });
    if (!resp.ok || !resp.body) throw new Error("chat → " + resp.status);
    const reader = resp.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, idx); buf = buf.slice(idx + 2);
        const dl = block.split("\n").find((l) => l.startsWith("data:"));
        if (!dl) continue;
        let obj; try { obj = JSON.parse(dl.slice(5).trim()); } catch (_e) { continue; }
        if (obj.open) { openPaper(obj.open); }          // server asked the UI to open a paper's PDF
        if (obj.status) { status(obj.status); }
        if (obj.refresh) { reloadLists(); }             // a paper was added: counts/lists changed
        if (obj.text) { raw += obj.text; bubble.innerHTML = renderMd(raw); $("#transcript").scrollTop = $("#transcript").scrollHeight; }
        if (obj.done) { /* stream end */ }
      }
    }
  } catch (err) {
    raw += `\n\n_(${err})_`; bubble.innerHTML = renderMd(raw);
  } finally {
    bubble.classList.remove("typing");
    if (!raw.trim()) bubble.innerHTML = "<span class='muted'>(no response)</span>";
    chatHistory.push({ role: "assistant", text: raw });
    chatBusy = false; $("#chatsend").disabled = false; $("#chatinput").focus();
  }
}

$("#chatform").addEventListener("submit", (e) => {
  e.preventDefault();
  const v = $("#chatinput").value.trim();
  if (!v) return;
  $("#chatinput").value = ""; $("#chatinput").style.height = "auto";
  sendChat(v);
});
$("#chatinput").addEventListener("input", (e) => { e.target.style.height = "auto"; e.target.style.height = Math.min(e.target.scrollHeight, 140) + "px"; });
$("#chatinput").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#chatform").requestSubmit(); } });

// ===================== panes =====================
function switchPane(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.pane === name));
  document.querySelectorAll(".pane").forEach((p) => p.classList.toggle("active", p.id === "pane-" + name));
  if (name === "chat") $("#chatinput").focus(); else $("#q").focus();
}
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => switchPane(t.dataset.pane)));
$("#listfilter").addEventListener("input", (e) => renderLists(e.target.value));

// ===================== resizable columns =====================
// The saved widths are a preference; what is applied is that preference fitted to
// the current window, so the centre (search + chat) always keeps MIN_CENTER px.
// Clamping each side on its own was not enough: a layout dragged in a wide window
// and restored in a narrow one left left + right wider than the window, and
// minmax(0, 1fr) gave the centre 0px — the search box and chat simply vanished.
const MIN_SIDE = 160, MIN_CENTER = 360, GUTTERS = 12;
function setupResize() {
  const root = document.documentElement;
  const pref = { left: null, right: null };          // px; null = the CSS default
  const eff = { left: 0, right: 0 };                 // px actually applied
  try {                                              // restore a previously dragged layout
    pref.left = parseFloat(localStorage.getItem("papersWLeft")) || null;
    pref.right = parseFloat(localStorage.getItem("papersWRight")) || null;
  } catch (_e) {}
  const apply = () => {
    const W = window.innerWidth, room = W - GUTTERS - MIN_CENTER;
    let L = pref.left ?? 270, R = pref.right ?? Math.round(W * 0.46);
    if (L + R > room) {                              // shrink both sides in proportion
      const k = Math.max(room, 2 * MIN_SIDE) / (L + R);
      L = Math.max(MIN_SIDE, Math.floor(L * k));
      R = Math.max(MIN_SIDE, Math.floor(R * k));
    }
    eff.left = L; eff.right = R;
    root.style.setProperty("--w-left", L + "px");
    root.style.setProperty("--w-right", R + "px");
  };
  apply();
  window.addEventListener("resize", apply);
  document.querySelectorAll(".gutter").forEach((g) => {
    const side = g.dataset.resize;                   // "left" or "right"
    const other = side === "left" ? "right" : "left";
    g.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      g.setPointerCapture(e.pointerId);
      document.body.classList.add("dragging");
      if (pref[other] != null) pref[other] = eff[other];   // a drag adopts what is on screen
      const onMove = (ev) => {
        const W = window.innerWidth;
        let w = side === "left" ? ev.clientX : (W - ev.clientX);
        w = Math.max(MIN_SIDE, Math.min(w, W - eff[other] - GUTTERS - MIN_CENTER));
        pref[side] = w;
        apply();
      };
      const onUp = () => {
        document.body.classList.remove("dragging");
        document.removeEventListener("pointermove", onMove);
        document.removeEventListener("pointerup", onUp);
        try {
          localStorage.setItem("papersWLeft", pref.left ?? "");
          localStorage.setItem("papersWRight", pref.right ?? "");
        } catch (_e) {}
      };
      document.addEventListener("pointermove", onMove);
      document.addEventListener("pointerup", onUp);
    });
  });
}
setupResize();

async function reloadLists() {
  try {
    LISTS = await api("/api/lists");
    const idx = await api("/api/index");
    INDEX = {};
    for (const r of idx) INDEX[r.name] = r;
    renderLists($("#listfilter").value);
  } catch (e) { $("#lists").textContent = String(e); }
}

// ===================== boot =====================
(async function boot() {
  try {
    const cfg = await api("/api/config");
    CONFIG = cfg;
    $("#lib").textContent = cfg.lib;
    if (cfg.chat) $("#chatstate").textContent = `chat: ${cfg.model} · library + ADS` + (cfg.writable ? " · can add" : " · read-only library");
    else { $("#chatstate").textContent = "chat: off"; $("#chatstate").title = cfg.chat_why || "";
           $("#chattab").disabled = true; $("#chattab").title = cfg.chat_why || "chat unavailable"; }
  } catch (e) {
    document.body.innerHTML = `<div style="padding:40px;color:#e88">Could not reach the server or the token is invalid.<br><small>${escapeHtml(String(e))}</small></div>`;
    return;
  }
  await reloadLists();
  $("#q").focus();
})();
