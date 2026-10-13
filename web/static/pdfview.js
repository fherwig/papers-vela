"use strict";
/* PDF viewer with marks, for the papers web GUI.

   Ported from the session-dashboard kit's viewer (kit 0.11.3, web/app.js: pdfFrame,
   annotLayer, annotSelection, regionSelection, annotAsk). The kit's comments record why each
   piece is the way it is; the short versions are kept here. What differs: a mark is one of the
   paper's COMMENTS in library.db (papers.add_comment), not a line in a session's .review/
   file, and the host page (app.js) loads and saves them; this file only draws and asks.

   Each page is a canvas with PDF.js's TEXT LAYER over it (transparent spans where the glyphs
   are), so selecting, copying and the browser's own find work as on any web page.

   window.PdfView.mount(host, opts) -> viewer
     opts.url        the PDF (token in the query string)
     opts.canMark    false on a read-only library: no ask, and the toggle says why
     opts.onMark(rec)   rec = {page, quote, note, rects, kind}; returns a Promise
     opts.onPin(cid)    a mark's ✎ pin was clicked
     opts.toast(msg)
   viewer.setMarks(items)  items = [{id, page, rects, note, quote}]; redraws the highlights
   viewer.focus(cid)       scroll to a mark and light it up
   viewer.slot             an element at the right end of the toolbar, for the host's controls
   viewer.destroy() */
(function () {
  const mk = (tag, cls, txt) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (txt != null) e.textContent = txt;
    return e;
  };

  // NOT `pdfjsLib`: the legacy build assigns globalThis.pdfjsLib when it loads and would
  // overwrite a same-named global of ours (kit, 2026-09-03: "pdfjsLib is not a function").
  let PDF_LIB = null;
  function loadPdfLib() {
    if (!PDF_LIB) {
      PDF_LIB = import("/vendor/pdfjs/pdf.min.mjs").then((m) => {
        m.GlobalWorkerOptions.workerSrc = "/vendor/pdfjs/pdf.worker.min.mjs";
        return m;
      });
      PDF_LIB.catch(() => { PDF_LIB = null; });       // a failed load must not be cached
    }
    return PDF_LIB;
  }

  const SCALE0 = 1.3, MODE_KEY = "papers.pdf.mode", MARKING_KEY = "papers.marking";
  const markingOn = () => localStorage.getItem(MARKING_KEY) !== "off";

  // One rect per LINE, not per glyph run: a selection across a formula comes back from
  // getClientRects() as dozens of fragments; rects whose vertical centres overlap are one line.
  function mergeLineRects(rects) {
    const rs = rects.filter((r) => r[2] - r[0] >= 1 && r[3] - r[1] >= 1)
                    .sort((a, b) => (a[1] + a[3]) - (b[1] + b[3]));
    const out = [];
    for (const r of rs) {
      const cy = (r[1] + r[3]) / 2, h = r[3] - r[1];
      const line = out.find((L) => Math.abs(cy - (L[1] + L[3]) / 2) < Math.max(h, L[3] - L[1]) * 0.6);
      if (!line) { out.push([...r]); continue; }
      line[0] = Math.min(line[0], r[0]); line[1] = Math.min(line[1], r[1]);
      line[2] = Math.max(line[2], r[2]); line[3] = Math.max(line[3], r[3]);
    }
    return out.map((r) => r.map((v) => Math.round(v * 100) / 100));
  }

  // fit: page width to the pane · page: one whole page in the pane's height · else manual
  function pdfFit(mode, base, avail) {
    if (!base || !base.width || !base.height) return SCALE0;
    const w = Math.max(80, avail.width), h = Math.max(120, avail.height);
    if (mode === "fit") return Math.max(0.4, Math.min(4, w / base.width));
    if (mode === "page") return Math.max(0.4, Math.min(4, Math.min(w / base.width, h / base.height)));
    return SCALE0;
  }

  // What is selected, as a mark needs it: page, text, and rects in page space at scale 1.
  // null unless the whole selection sits on ONE page: a drag across two pages is two marks.
  function annotSelection(box) {
    const sel = window.getSelection && window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
    const range = sel.getRangeAt(0);
    const at = (n) => n && (n.nodeType === 1 ? n : n.parentElement);
    const a = at(range.startContainer) && at(range.startContainer).closest(".pdfv-page");
    const b = at(range.endContainer) && at(range.endContainer).closest(".pdfv-page");
    if (!a || a !== b || !box.contains(a)) return null;
    const scale = parseFloat(a.style.getPropertyValue("--scale-factor")) || 1;
    const pr = a.getBoundingClientRect();
    const rects = [];
    for (const r of range.getClientRects()) {
      if (r.width < 1 || r.height < 1) continue;
      rects.push([(r.left - pr.left) / scale, (r.top - pr.top) / scale,
                  (r.right - pr.left) / scale, (r.bottom - pr.top) / scale]);
    }
    const quote = String(sel).replace(/\s+/g, " ").trim();
    if (!rects.length || !quote) return null;
    return { page: parseInt(a.dataset.page, 10), rects: mergeLineRects(rects), quote, pageEl: a, kind: "text" };
  }

  // A REGION mark: a box dragged on the page, for what text selection cannot hold (formulas,
  // tables, figures). The quote is the text of the spans the box covers.
  function regionSelection(pageEl, x0, y0, x1, y1) {
    const scale = parseFloat(pageEl.style.getPropertyValue("--scale-factor")) || 1;
    const L = Math.min(x0, x1), T = Math.min(y0, y1), R = Math.max(x0, x1), B = Math.max(y0, y1);
    if (R - L < 4 || B - T < 4) return null;
    const pr = pageEl.getBoundingClientRect();
    const words = [];
    for (const sp of pageEl.querySelectorAll(".textLayer span")) {
      const r = sp.getBoundingClientRect();
      const l = r.left - pr.left, t = r.top - pr.top, rr = r.right - pr.left, b = r.bottom - pr.top;
      if (rr > L && l < R && b > T && t < B && sp.textContent.trim()) words.push(sp.textContent);
    }
    const quote = words.join(" ").replace(/\s+/g, " ").trim() || `[region on page ${pageEl.dataset.page}]`;
    const rects = [[L / scale, T / scale, R / scale, B / scale].map((v) => Math.round(v * 100) / 100)];
    return { page: parseInt(pageEl.dataset.page, 10), rects, quote, pageEl, kind: "region" };
  }

  function mount(host, opts) {
    const toast = opts.toast || (() => {});
    const box = mk("div", "pdfv-box");
    const bar = mk("div", "pdfv-bar");
    const chip = (txt, title, fn) => { const b = mk("button", "pbtn", txt); b.title = title; if (fn) b.onclick = fn; return b; };
    const cFit = chip("fit", "Fit the page to the width of this pane");
    const cPage = chip("page", "One whole page at a time");
    const lbl = mk("span", "pdfv-scale", "");
    const cRegion = chip("▭", "Mark a REGION: drag a box on the page (or hold Alt while dragging). "
                              + "For formulas, tables and figures, where selecting text is unreliable.");
    const cMark = chip("", "");
    const slot = mk("span", "pdfv-slot");            // the host page's actions, right-aligned
    bar.append(cFit, cPage, chip("⊖", "zoom out", () => zoom(0.8)), chip("⊕", "zoom in", () => zoom(1.25)),
               lbl, cRegion, cMark, chip("⟳", "Reload the PDF", () => reload()), slot);
    const pages = mk("div", "pdfv-pages");
    const note = mk("div", "pdfv-note", "rendering…");
    pages.append(note);
    box.append(bar, pages);
    host.append(box);

    let scale = SCALE0, doc = null, gen = 0, base = null, bust = 0, region = false, dead = false;
    let mode = localStorage.getItem(MODE_KEY) || "fit";
    let marks = [];

    const paintMark = () => {
      const on = opts.canMark && markingOn();
      cMark.textContent = "✎ mark";
      cMark.classList.toggle("off", !on);
      cMark.classList.toggle("on", on);
      cMark.disabled = !opts.canMark;
      cMark.title = !opts.canMark ? "This library is read-only, so marks can't be saved"
        : on ? "Selecting text offers to mark it as a comment. Click to turn that off, so a selection "
               + "can be copied (Enter over a selection, or a dragged region, still marks)"
             : "Selecting text only selects it. Click to make a selection offer a mark again";
    };
    cMark.onclick = () => {
      localStorage.setItem(MARKING_KEY, markingOn() ? "off" : "on");
      if (!markingOn()) box.querySelectorAll(".pdfv-ask").forEach((n) => n.remove());
      document.querySelectorAll(".pdfv-box").forEach((b) => b._paintMark && b._paintMark());
    };
    box._paintMark = paintMark;
    cRegion.onclick = () => {
      if (!opts.canMark) return toast("this library is read-only — marks can't be saved");
      region = !region; box.classList.toggle("region", region); cRegion.classList.toggle("on", region);
    };
    const paintBar = () => {
      cFit.classList.toggle("on", mode === "fit"); cPage.classList.toggle("on", mode === "page");
      lbl.textContent = Math.round(scale * 100) + "%";
      paintMark();
    };
    const avail = () => ({ width: pages.clientWidth - 24, height: pages.clientHeight - 24 });

    // ---- marks: highlights + a ✎ pin per noted mark, in page space at scale 1 ----
    function annotLayer(page, sc) {
      const layer = mk("div", "pdfv-notes");
      for (const a of marks) {
        if (a.page !== page) continue;
        for (const r of a.rects || []) {
          const h = mk("div", "pdfv-hl");
          h.style.left = r[0] * sc + "px"; h.style.top = r[1] * sc + "px";
          h.style.width = (r[2] - r[0]) * sc + "px"; h.style.height = (r[3] - r[1]) * sc + "px";
          h.dataset.cid = a.id;
          if (a.note) h.title = a.note;
          layer.append(h);
        }
        if (a.note && a.rects && a.rects.length) {
          const r = a.rects[0];
          const pin = mk("div", "pdfv-pin", "✎");
          pin.style.left = Math.max(0, r[0] * sc - 20) + "px"; pin.style.top = r[1] * sc + "px";
          pin.title = a.note;
          pin.onclick = (ev) => { ev.stopPropagation(); focus(a.id); opts.onPin && opts.onPin(a.id); };
          layer.append(pin);
        }
      }
      return layer;
    }
    function redrawMarks() {
      pages.querySelectorAll(".pdfv-page").forEach((pg) => {
        pg.querySelectorAll(".pdfv-notes").forEach((n) => n.remove());
        pg.append(annotLayer(parseInt(pg.dataset.page, 10),
                             parseFloat(pg.style.getPropertyValue("--scale-factor")) || 1));
      });
    }
    function focus(cid) {
      pages.querySelectorAll(".pdfv-hl.on").forEach((h) => h.classList.remove("on"));
      const hs = pages.querySelectorAll(`.pdfv-hl[data-cid="${cid}"]`);
      hs.forEach((h) => h.classList.add("on"));
      if (hs[0]) hs[0].scrollIntoView({ block: "center" });
    }

    // ---- the ask beside a selection: one optional line, one button ----
    function ask(given) {
      if (!opts.canMark) { toast("this library is read-only — marks can't be saved"); return false; }
      const sel = given || annotSelection(box);
      if (!sel) { toast("select some text on ONE page first — or drag a region with ▭ / Alt"); return false; }
      box.querySelectorAll(".pdfv-ask").forEach((n) => n.remove());
      const a = mk("div", "pdfv-ask");
      const inp = mk("input");
      inp.placeholder = sel.kind === "region"
        ? "comment (optional) — region: " + sel.quote.slice(0, 40) + (sel.quote.length > 40 ? "…" : "")
        : "comment (optional) — Enter to mark, Esc to cancel";
      const go = mk("button", "pbtn", "✎ mark"), x = mk("button", "pbtn", "✕");
      a.append(inp, go, x);
      const sc = parseFloat(sel.pageEl.style.getPropertyValue("--scale-factor")) || 1;
      const last = sel.rects[sel.rects.length - 1];
      a.style.left = Math.max(4, last[0] * sc) + "px";
      a.style.top = (last[3] * sc + 6) + "px";
      sel.pageEl.append(a);
      const done = () => a.remove();
      const submit = async () => {
        go.disabled = true;
        try {
          await opts.onMark({ page: sel.page, quote: sel.quote, note: inp.value.trim(), rects: sel.rects, kind: sel.kind });
          window.getSelection && window.getSelection().removeAllRanges();
          done();
        } catch (e) { toast("not saved — " + (e.message || e)); go.disabled = false; }
      };
      go.onclick = submit; x.onclick = done;
      inp.onkeydown = (ev) => {
        ev.stopPropagation();                        // the field owns its keys
        if (ev.key === "Enter") { ev.preventDefault(); submit(); }
        if (ev.key === "Escape") { ev.preventDefault(); done(); }
      };
      inp.focus();
      return true;
    }
    box._ask = ask;
    pages.addEventListener("mouseup", () => setTimeout(() => {
      if (box._rubberDone) { box._rubberDone = false; return; }  // a region drag is not a selection
      if (opts.canMark && markingOn() && annotSelection(box)) ask();
    }, 0));

    // ---- region mode: rubber band on a page (▭ toggled, or Alt held) ----
    pages.addEventListener("pointerdown", (e) => {
      if (e.button !== 0 || !(region || e.altKey) || !opts.canMark) return;
      const pg = e.target.closest(".pdfv-page"); if (!pg) return;
      e.preventDefault();
      const pr = pg.getBoundingClientRect();
      const x0 = e.clientX - pr.left, y0 = e.clientY - pr.top;
      const rb = mk("div", "pdfv-rubber"); pg.append(rb);
      const at = (x, y) => { rb.style.left = Math.min(x0, x) + "px"; rb.style.top = Math.min(y0, y) + "px";
                             rb.style.width = Math.abs(x - x0) + "px"; rb.style.height = Math.abs(y - y0) + "px"; };
      at(x0, y0);
      pg.setPointerCapture(e.pointerId);
      const move = (ev) => at(ev.clientX - pr.left, ev.clientY - pr.top);
      const stop = (ev) => {
        pg.removeEventListener("pointermove", move); pg.removeEventListener("pointerup", stop);
        removeEventListener("keydown", esc, true);
        try { pg.releasePointerCapture(e.pointerId); } catch (_) {}
        rb.remove();
        if (ev.type === "pointerup") {
          const sel = regionSelection(pg, x0, y0, ev.clientX - pr.left, ev.clientY - pr.top);
          box._rubberDone = true;
          if (sel) ask(sel); else toast("region too small — drag a larger box");
        }
      };
      const esc = (ev) => { if (ev.key === "Escape") { ev.stopPropagation(); stop({ type: "cancel" }); } };
      pg.addEventListener("pointermove", move); pg.addEventListener("pointerup", stop);
      addEventListener("keydown", esc, true);
    });

    // ---- rendering ----
    const fallback = (why, err) => {
      if (err) console.error("pdf viewer fell back:", (err && err.stack) || err);
      pages.textContent = "";
      pages.append(mk("div", "pdfv-note", "PDF.js could not render this file (" + why + ") — showing the "
                      + "browser's own viewer instead. Marks are not available there."));
      const f = mk("iframe", "pdfv-fallback"); f.src = opts.url; pages.append(f);
    };

    /* Draw every page at the current scale. Pages are appended as each finishes, so page 1 is
       readable while page 20 still parses; a zoom mid-render bumps `gen` and stale pages are
       dropped. New pages render inside an attached, invisible STAGE (TextLayer reads computed
       style, which a detached node lacks) and the old pages stay until the first new one is
       ready, so a zoom never blanks the document. Canvases are painted lazily, near the view;
       text layers are built for every page so find and marks see the whole document. */
    let io = null;
    const draw = async () => {
      const my = ++gen;
      const lib = await loadPdfLib();
      if (!doc) {
        // disableFontFace: PDF.js draws glyph outlines itself; macOS CoreText otherwise clips
        // the tops of display-math glyphs (mozilla/pdf.js#20165; kit, 2026-09-21)
        doc = await lib.getDocument({ url: opts.url + (bust ? "&v=" + bust : ""),
          standardFontDataUrl: "/vendor/pdfjs/standard_fonts/", disableFontFace: true }).promise;
      }
      if (my !== gen || dead) return;
      const keepTop = pages.scrollTop / Math.max(1, pages.scrollHeight);
      const dpr = window.devicePixelRatio || 1;
      const stage = mk("div", "pdfv-stage");
      pages.append(stage);
      const old = [...pages.querySelectorAll(":scope > .pdfv-page, :scope > .pdfv-note")];
      let swapped = false;
      if (io) io.disconnect();
      const paint = async (pg) => {
        if (pg._painted === my || my !== gen) return;
        pg._painted = my;
        const { page, vp } = pg._src;
        const cv = mk("canvas");
        cv.width = Math.floor(vp.width * dpr); cv.height = Math.floor(vp.height * dpr);
        cv.style.width = vp.width + "px"; cv.style.height = vp.height + "px";
        const ctx = cv.getContext("2d"); ctx.scale(dpr, dpr);
        await page.render({ canvasContext: ctx, viewport: vp }).promise;
        if (my !== gen) return;
        const prev = pg.querySelector(":scope > canvas");
        if (prev) prev.replaceWith(cv); else pg.prepend(cv);
      };
      io = new IntersectionObserver((entries) => {
        for (const en of entries) if (en.isIntersecting) paint(en.target).catch(() => {});
      }, { root: pages, rootMargin: "100% 0px" });
      for (let n = 1; n <= doc.numPages; n++) {
        const page = await doc.getPage(n);
        if (my !== gen || dead) return;
        if (n === 1) {
          const v1 = page.getViewport({ scale: 1 });
          base = { width: v1.width, height: v1.height };
          if (mode !== "manual") scale = pdfFit(mode, base, avail());
          paintBar();
        }
        const vp = page.getViewport({ scale });
        const pg = mk("div", "pdfv-page");
        pg.dataset.page = String(n);
        pg.style.width = vp.width + "px"; pg.style.height = vp.height + "px";
        // both: TextLayer's placement reads --scale-factor, its sizing --total-scale-factor
        pg.style.setProperty("--scale-factor", String(vp.scale));
        pg.style.setProperty("--total-scale-factor", String(vp.scale));
        const tl = mk("div", "textLayer");
        pg.append(tl);
        pg._src = { page, vp };
        stage.append(pg);
        if (n === 1) { await paint(pg); if (my !== gen) { stage.remove(); return; } }
        await new lib.TextLayer({ textContentSource: page.streamTextContent(), container: tl, viewport: vp }).render();
        if (my !== gen) { stage.remove(); return; }
        // PDF.js's selection guard: a span-less full-page target while the button is down, so a
        // drag past the glyphs ends at the line instead of jumping to the nearest span
        tl.append(mk("div", "endOfContent"));
        tl.addEventListener("mousedown", () => tl.classList.add("selecting"));
        if (!swapped) { old.forEach((o) => o.remove()); swapped = true; }
        pg.append(annotLayer(n, vp.scale));
        pages.insertBefore(pg, stage);
        io.observe(pg);
        if (n === 1) pages.scrollTop = keepTop * pages.scrollHeight;
      }
      stage.remove();
    };
    const redraw = () => draw().catch((e) => fallback(String(e.message || e), e));
    const setMode = (m) => { mode = m; localStorage.setItem(MODE_KEY, m); paintBar(); redraw(); };
    cFit.onclick = () => setMode("fit"); cPage.onclick = () => setMode("page");
    const zoom = (f) => { mode = "manual"; scale = Math.max(0.4, Math.min(4, scale * f)); paintBar(); redraw(); };
    const reload = () => { doc = null; bust = Date.now(); redraw(); };

    // the pane changes width with the window AND with the dividers: a fit mode follows it
    let lastW = 0, rt = null;
    const ro = new ResizeObserver(() => {
      const w = pages.clientWidth;
      if (!w || Math.abs(w - lastW) < 8) return;     // hidden tab, or no real change
      const first = !lastW; lastW = w;
      if (first || mode === "manual") return;
      clearTimeout(rt); rt = setTimeout(redraw, 180);
    });
    ro.observe(pages);
    paintBar();
    redraw();

    return {
      el: box,
      slot,
      setMarks(items) { marks = (items || []).filter((m) => m.page && m.rects && m.rects.length); redrawMarks(); },
      focus,
      destroy() { dead = true; gen++; ro.disconnect(); if (io) io.disconnect(); if (doc) doc.destroy(); box.remove(); },
    };
  }

  // Enter over a selection marks it even with auto-asking off (a deliberate act copying never does)
  addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || /^(INPUT|TEXTAREA)$/.test((document.activeElement || {}).tagName || "")) return;
    const sel = window.getSelection && window.getSelection();
    const n = sel && sel.anchorNode;
    const box = n && (n.nodeType === 1 ? n : n.parentElement) && (n.nodeType === 1 ? n : n.parentElement).closest(".pdfv-box");
    if (box && box._ask && !sel.isCollapsed) { e.preventDefault(); box._ask(); }
  });
  // release the selection guard on every text layer when the button comes up
  addEventListener("mouseup", () => document.querySelectorAll(".textLayer.selecting")
    .forEach((t) => t.classList.remove("selecting")));

  window.PdfView = { mount };
})();
