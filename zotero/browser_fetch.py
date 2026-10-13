"""browser_fetch.py — grab a paywalled version-of-record PDF through the user's
OWN authenticated browser session, with no Accessibility/keystroke permissions.

The idea (works for any browser that renders PDFs and keeps a disk cache):
  1. Open the publisher PDF URL in a NEW browser window via the browser's own
     CLI/remote command — this reuses the running profile (cookies + any
     Cloudflare clearance), so it passes paywalls/bot-walls that curl cannot.
  2. The browser renders the PDF and writes the bytes to its disk cache.
     Poll the cache for a fresh `%PDF…%%EOF` whose bytes carry the DOI.
  3. Close that window (a plain Apple event — no GUI scripting / keystrokes).

None of this uses a special tool: opening is the browser's shell binary, closing
is `osascript`. It is all ordinary shell.

Choosing a browser
------------------
The browser is NOT hardwired. Pick one with the `PAPERS_BROWSER` env var
(default "firefox"); override its binary with `PAPERS_BROWSER_BIN`. Each browser
is a small backend class below — the open/cache/close commands differ per browser,
so adding Chrome/Safari/etc. later means adding one backend class, nothing else.
If a user asks for a browser that has no backend yet, we say so and fall back to
the caller's normal download chain (a new backend has to be written for it).

Disable entirely with PAPERS_BROWSER_FETCH=0. No-ops off macOS.
"""
import os, re, sys, glob, time, subprocess


# ── shared: carve a PDF out of one cache entry ─────────────────────────────
def carve_pdf(data):
    """Return PDF bytes from a browser cache entry (PDF body + any metadata
    trailer), or None. The body runs from %PDF to the LAST %%EOF."""
    if not data.startswith(b"%PDF"):
        return None
    i = data.rfind(b"%%EOF")
    if i == -1:
        return None
    end = i + 5
    if end < len(data) and data[end:end + 1] in (b"\n", b"\r"):
        end += 1
    return data[:end]


def _osa(script):
    return subprocess.run(["osascript", "-e", script], capture_output=True, text=True)


# APS journal token (lowercased, as in a DOI) → (URL abbreviation, proper case).
# APS PDF URLs are CASE-SENSITIVE in the journal token; our DOIs are stored
# lowercased, so we must restore the proper case here or the URL 404s.
_APS = {
    "physrevlett": ("prl", "PhysRevLett"), "physrevx": ("prx", "PhysRevX"),
    "physrevc": ("prc", "PhysRevC"), "physrevd": ("prd", "PhysRevD"),
    "physreva": ("pra", "PhysRevA"), "physrevb": ("prb", "PhysRevB"),
    "physreve": ("pre", "PhysRevE"), "revmodphys": ("rmp", "RevModPhys"),
    "physrevresearch": ("prresearch", "PhysRevResearch"),
    "physrevapplied": ("prapplied", "PhysRevApplied"),
    "physrevfluids": ("prfluids", "PhysRevFluids"),
    "physrevmaterials": ("prmaterials", "PhysRevMaterials"),
    "physrevaccelbeams": ("prab", "PhysRevAccelBeams"),
    "physrevstab": ("prstab", "PhysRevSTAB"), "prxquantum": ("prxquantum", "PRXQuantum"),
}


def publisher_pdf_url(doi):
    """Direct version-of-record PDF URL from a DOI, for publishers with a stable
    URL pattern. Returns None when we don't know the pattern (caller then falls
    back to the DOI link itself). Add a publisher here as needed.

    The APS branch restores the case of the journal token (DOIs are stored
    lowercased, but APS PDF URLs are case-sensitive), e.g. a DOI of the form
    10.1103/physrevc.<vol>.<id> -> https://journals.aps.org/prc/pdf/10.1103/PhysRevC.<vol>.<id>
    """
    if not doi:
        return None
    d = doi.lower().strip()
    rest = d.split("/", 1)[1] if "/" in d else ""
    if d.startswith("10.1103/"):                       # APS (Physical Review)
        ab = _APS.get(rest.split(".", 1)[0])
        if not ab:
            return None
        abbr, proper = ab
        rest_proper = proper + rest[len(rest.split(".", 1)[0]):]   # restore case
        return f"https://journals.aps.org/{abbr}/pdf/10.1103/{rest_proper}"
    if d.startswith("10.3847/"):                       # IOP (ApJ/AJ/ApJS/PASP)
        return f"https://iopscience.iop.org/article/{d}/pdf"
    if d.startswith("10.1007/"):                       # Springer
        return f"https://link.springer.com/content/pdf/{d}.pdf"
    return None


# ── backend interface ──────────────────────────────────────────────────────
class BrowserBackend:
    """Subclass per browser. A backend knows how to open a URL in a new window,
    find the PDF the browser cached, and close the window it opened."""
    name = "base"

    def available(self):
        raise NotImplementedError

    def fetch(self, doi, bibcode, dest, verbose=False, load_wait=20):
        raise NotImplementedError

    @staticmethod
    def candidate_urls(doi, bibcode):
        urls = []
        pub = publisher_pdf_url(doi)                    # direct publisher PDF (best)
        if pub:
            urls.append(pub)
        if doi:            # the DOI link — routes to the publisher reliably (fallback
            urls.append(f"https://doi.org/{doi}")      # for publishers w/o a pattern)
        if bibcode:        # ADS gateway → authenticated redirect (works for A&A/EDP)
            urls.append(f"https://ui.adsabs.harvard.edu/link_gateway/{bibcode}/PUB_PDF")
        return urls


class FirefoxBackend(BrowserBackend):
    name = "firefox"
    DEFAULT_BIN = "/Applications/Firefox.app/Contents/MacOS/firefox"
    PROC = "firefox"

    def __init__(self):
        self.bin = os.environ.get("PAPERS_BROWSER_BIN", self.DEFAULT_BIN)

    def available(self):
        return (sys.platform == "darwin" and os.path.exists(self.bin)
                and subprocess.run(["pgrep", "-x", self.PROC],
                                   capture_output=True).returncode == 0)

    # -- window control via the firefox binary (open) + Apple events (close) --
    def _window_ids(self):
        out = _osa('tell application "Firefox" to get id of every window').stdout
        return {int(n) for n in re.findall(r"-?\d+", out) if int(n) > 0}

    def _open_new_window(self, url):
        before = self._window_ids()
        subprocess.run([self.bin, "--new-window", url], capture_output=True, timeout=30)
        for _ in range(20):
            time.sleep(0.5)
            new = self._window_ids() - before
            if new:
                return new.pop()
        return None

    def _close_window(self, win_id):
        if win_id is not None:
            _osa(f'tell application "Firefox" to close (every window whose id is {win_id})')

    def _cache_dir(self):
        c = glob.glob(os.path.expanduser(
            "~/Library/Caches/Firefox/Profiles/*/cache2/entries"))
        return c[0] if c else None

    def _scan_cache(self, cache_dir, since_ts, doi):
        needle = doi.lower().encode() if doi else None
        fallback = None
        for name in os.listdir(cache_dir):
            p = os.path.join(cache_dir, name)
            try:
                if os.path.getmtime(p) < since_ts:
                    continue
                with open(p, "rb") as fh:
                    if fh.read(4) != b"%PDF":
                        continue
                    data = b"%PDF" + fh.read()
            except OSError:
                continue
            pdf = carve_pdf(data)
            if not pdf:
                continue
            if needle and needle in pdf.lower():
                return pdf
            fallback = fallback or pdf
        return fallback

    def fetch(self, doi, bibcode, dest, verbose=False, load_wait=20):
        cache = self._cache_dir()
        if not cache:
            return False, None
        for url in self.candidate_urls(doi, bibcode):
            since = time.time() - 1
            if verbose:
                print(f"    browser[{self.name}]: opening {url}")
            win = self._open_new_window(url)
            pdf = None
            for _ in range(load_wait * 2):
                time.sleep(0.5)
                pdf = self._scan_cache(cache, since, doi)
                if pdf:
                    break
            self._close_window(win)          # close the window after grabbing
            if pdf:
                with open(dest, "wb") as f:
                    f.write(pdf)
                if os.path.getsize(dest) > 5000 and pdf.startswith(b"%PDF"):
                    return True, f"{self.name}-cache"
                try: os.remove(dest)
                except OSError: pass
        return False, None


# Registry — add a browser by adding its backend here.
BACKENDS = {b.name: b for b in [FirefoxBackend()]}


def get_backend():
    name = os.environ.get("PAPERS_BROWSER", "firefox").lower()
    return BACKENDS.get(name), name


def fetch_via_browser(doi, bibcode, dest, verbose=False):
    """Try to obtain the version-of-record PDF via the user's browser.
    Returns (ok, label). No-ops (False, None) when disabled, off-macOS, the
    chosen browser has no backend, or the browser isn't running."""
    if os.environ.get("PAPERS_BROWSER_FETCH", "1") == "0":
        return False, None
    backend, name = get_backend()
    if backend is None:
        if verbose:
            print(f"    browser: '{name}' has no backend yet "
                  f"(supported: {', '.join(sorted(BACKENDS))}). "
                  f"Add one in browser_fetch.py to use it.")
        return False, None
    if not backend.available():
        if verbose:
            print(f"    browser[{name}]: not available/running — skipping")
        return False, None
    return backend.fetch(doi, bibcode, dest, verbose=verbose)
