#!/usr/bin/env python3
"""
zotero_sync.py — keep a papers-vela library in sync with a running Zotero.

Syncs in two directions:
  1. New Zotero items → insert into papers-vela (with classification)
  2. Papers already in papers-vela that now have a Zotero PDF → update pdf_path

Safe to run while Zotero is open: reads the live DB with immutable WAL mode;
falls back to zotero.sqlite.bak if that is locked.

Usage:
  python3 zotero_sync.py [--once | --watch [--interval N]] [--fetch-pdfs] [--dry-run] [--verbose]

Env vars (all have defaults):
  PAPERS_LIB    papers-vela library dir   (default ~/Zotero/papers-vela)
  PAPERS_VELA  papers-vela repo dir      (default ~/papers-vela)
  ZOTERO_DB     path to zotero.sqlite      (default ~/Zotero/zotero.sqlite)
  ZOTERO_DIR    Zotero base dir            (default ~/Zotero)
"""

import argparse, datetime, json, os, random, re, shutil, sqlite3, string, subprocess, sys, tempfile, time, unicodedata

# PAPERS_VELA (the repo clone) defaults to this script's own repo; PAPERS_LIB is required (no default).
PAPERS_VELA = os.environ.get("PAPERS_VELA") or os.environ.get("PAPERS_AGENT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.expanduser(PAPERS_VELA))
from papers import clean_text, last_name, keywords as make_keywords, reindex_lists, require_lib

LIB        = os.path.expanduser(os.environ.get("PAPERS_LIB") or "")   # REQUIRED, no default
DB         = os.path.join(LIB, "library.db")
ZOTERO_DB  = os.path.expanduser(os.environ.get("ZOTERO_DB",  "~/Zotero/zotero.sqlite"))
ZOTERO_BAK = ZOTERO_DB + ".bak"
ZOTERO_DIR = os.path.expanduser(os.environ.get("ZOTERO_DIR", "~/Zotero"))
STATE_FILE = os.path.join(LIB, ".zotero_sync_state.json")

# Item types in Zotero we care about
ZOTERO_TYPES = (
    "journalArticle", "preprint", "conferencePaper",
    "book", "bookSection", "thesis", "report",
)


# ---------------------------------------------------------------------------
# Classification (same rules as zotero_import.py — keep in sync)
# ---------------------------------------------------------------------------

RULES = [
    ("i process",          ["i-process", " i process ", "intermediate neutron-capture"]),
    ("s process",          ["s-process", "slow neutron capture", "slow-neutron"]),
    ("r process",          ["r-process", "rapid neutron capture", "rapid-neutron"]),
    ("p process",          ["p-process", "photodisintegration", "gamma-process", " p process "]),
    ("n process",          [" n-process", " n process "]),
    ("CEMP-no",            ["cemp-no", "cemp no "]),
    ("CEMP-r/s",           ["cemp-r/s", "cemp-rs "]),
    ("Sakurai's object",   ["sakurai", "v4334 sgr", "born-again agb", "very late thermal pulse"]),
    ("RCB",                ["r coronae borealis", "rcb star", "hydrogen-deficient carbon star"]),
    ("Pair-instability",   ["pair instability", "pair-instability", " pisn ", "pulsational pair"]),
    ("CE & WD merger",     ["common envelope", "common-envelope evolution"]),
    ("thermohaline mixing",["thermohaline"]),
    ("double convection",  ["doubly diffusive", "semiconvection", "double diffusive"]),
    ("convective-reactive nucl", ["convective-reactive", "h ingestion", "proton ingestion", "h-ingestion"]),
    ("shell mergers",      ["shell merger", "shell convection merger"]),
    ("nuclear physics uncertainty", ["reaction rate uncertainty", "nuclear uncertainty", "rate sensitivity study"]),
    ("OCen and other GCs", ["omega centauri", "ω cen", "globular cluster"]),
    ("Planetary Nebulae",  ["planetary nebul", "proto-planetary nebul", "protoplanetary nebul"]),
    ("super-AGB",          ["super-agb", "super agb", "super-asymptotic giant"]),
    ("post-AGB",           ["post-agb", "post agb ", "post-asymptotic giant"]),
    ("AGB stars",          ["asymptotic giant branch", "agb star", "thermally pulsing", "tp-agb", "tpagb"]),
    ("White dwarfs",       ["white dwarf", "white-dwarf", "initial-final mass relation", "ifmr"]),
    ("nova and rawds",     [" novae ", " nova ", "classical nova"]),
    ("SNIa, DD merger",    ["type ia supernova", "type-ia supernova", " sn ia ", " snia ",
                            "thermonuclear supernova", "double detonation", "type ia supernovae"]),
    ("Supernovae",         ["core-collapse supernova", " ccsn ", "core collapse supernova",
                            "supernova remnant", "cassiopeia a", " cas a ", "supernova 1987a",
                            "core-collapse sn", "type ii supernova", "type ib", "type ic",
                            "superluminous supernova", " sn 2", "core-collapse simulation",
                            "sn1987a", " sn 1987"]),
    ("Massive stars",      ["massive star", "o-type star", "wolf-rayet", "w-r star", "ob star",
                            "blue supergiant", "luminous blue variable", " lbv ", "red supergiant"]),
    ("Pop III stars",      ["pop iii", "population iii", "first stars", "zero metallicity star",
                            "zero-metallicity star", "metal-free star"]),
    ("super-massive stars",["super-massive star", "very massive star", "supermassive star"]),
    ("metal-poor stars",   ["metal-poor star", "metal poor star", "extremely metal-poor", "emp star",
                            "very metal-poor", "halo star", "cemp star", "[fe/h] < -2", "[fe/h]<-2",
                            "low-metallicity star", "metal-poor halo", "metal-poor"]),
    ("XRB",                ["x-ray binary", " xrb ", "accreting neutron star", "x-ray burst "]),
    ("neutron star merger/accretion", ["neutron star merger", "kilonova", "ns-ns merger", "bns merger",
                            "compact binary merger"]),
    ("gravitational waves", ["gravitational wave", " ligo ", " virgo ", "gw150914", "gw event",
                             "gw detection"]),
    ("massive BHs",        ["black hole", "black-hole", " agn ", "quasar"]),
    ("binaries",           ["binary star", "binary system", "mass transfer", "roche lobe overflow",
                            "roche-lobe", "interacting binary", "close binary", "binaries: close"]),
    ("pre-solar grains",   ["presolar grain", "pre-solar grain", "stardust grain", " sic grain",
                            "silicon carbide grain", "graphite grain", "carbonaceous grain",
                            "circumstellar grain"]),
    ("Radioactive",        [" 26al ", " 60fe ", " 44ti", "radioactive isotope",
                            "gamma-ray line", "gamma ray line", "radioactive nuclei",
                            "radioactive scandium", "44ti-powered"]),
    ("Nuclear physics",    ["nuclear reaction rate", "cross section", "astrophysical s-factor",
                            "thermonuclear reaction rate", "direct capture", "radiative capture",
                            " neutrino ", "cno cycle", "nuclear physics", "nuclear astrophysics",
                            "thermonuclear reaction"]),
    ("GCE",                ["galactic chemical evolution", "chemical evolution model",
                            "milky way chemical", "chemical enrichment model", "galaxy chemical evolution",
                            "galactic evolution"]),
    ("Yields",             ["stellar yield", "nucleosynthesis yield", "chemical yield", "yield table"]),
    ("Nucleosynthesis",    ["nucleosynthesis", "origin of elements", "element formation",
                            "synthesis of the elements"]),
    ("overshoot",          ["convective overshooting", "convective boundary mixing",
                            "overshooting parameter", "core overshooting", "cbm "]),
    ("IGWs",               ["internal gravity wave", " igw ", "gravity wave transport",
                            "wave-driven mixing", "wave angular momentum", "gravity waves"]),
    ("convection theory",  ["mixing-length", "mixing length theory", " mlt ", "turbulent convection theory",
                            "convection model", "convective model", "convection zone",
                            "wasserstoffkonvektionszone", "konvektionszone"]),
    ("3D stellar hydro",   ["3d simulation", "three-dimensional simulation", "3d convection",
                            "3d stellar", "three-dimensional stellar", "3d model of stellar"]),
    ("Stellar hydro",      ["hydrodynamic simulation", "stellar hydrodynamics", "multidimensional stellar"]),
    ("hydro & turbulence", ["turbulence", "kelvin-helmholtz", "shear instability", "turbulent mixing",
                            "rayleigh-taylor", "richtmyer-meshkov", "ausgebildeten turbulenz",
                            "turbulenz"]),
    ("rotation & secular", ["rotational mixing", "angular momentum transport", "stellar rotation",
                            "secular mixing", "meridional circulation", "spin-down", "tidal mixing"]),
    ("asteroseismology",   ["asteroseismology", " seismology", "stellar oscillation", "stellar pulsation",
                            "mode frequency", "gravity mode", " p-mode ", " g-mode ", "dipole mode",
                            "delta scuti", "pulsator"]),
    ("He-core-burning",    ["helium core burning", "core helium burning", "horizontal branch",
                            "red clump", "he-core", "helium-burning"]),
    ("Li RGB He-core",     [" lithium ", " li-rich", " li abundance", "li plateau", "rgb lithium"]),
    ("Mass loss",          ["mass loss", "stellar wind", "mass-loss rate", "agb wind", "stellar outflow",
                            "stars: mass-loss"]),
    ("main-sequence hydro",["main-sequence convection", "core convection on main",
                            "main sequence mixing", "convective core on main"]),
    ("RGB mixing",         ["rgb mixing", "first dredge-up", "extra mixing on rgb",
                            "red giant branch mixing"]),
    ("Radioastronomy",     ["radio telescope", "radio observation", " vla ", " alma ", " vlbi ",
                            "radio continuum", "synchrotron radiation", "radio source",
                            "radio-frequency radiation"]),
    ("Stellar Evolution",  ["stellar evolution", "stellar model", "stellar structure", "stellar interior",
                            "shell source star", "double shell source"]),
    ("stellar abundances", ["stellar abundance", "photospheric abundance", "stellar spectr",
                            "chemical composition of star", "chemical composition of the star",
                            "chemical composition of the sun", "solar abundanc",
                            "elemental abundance", "element abundance"]),
    ("cosmology",          ["dark matter", "dark energy", " cmb ", "big bang nucleosynthesis",
                            "cosmic microwave", " bbn ", "cosmological"]),
    ("Stellar populations",["stellar population", "isochrone age", "star cluster age",
                            "old stellar population"]),
    ("geoastronomy",       ["geoastronomy", "planet formation", "solar system formation",
                            "solar nebula", "planetary composition", "planetary interior",
                            "solar system: ", "protoplanetary disc", "protoplanetary disk"]),
    ("AIML",               ["machine learning", "neural network", "deep learning",
                            "artificial intelligence", "convolutional neural", "random forest",
                            "gradient boosting", "reinforcement learning"]),
    ("Biomodeling",        ["biological", " rna ", " dna ", "genome", "cardiovascular",
                            "clinical trial", "gene expression"]),
    ("cyber infrastructure",["mesa ", "modules for experiments", "stellar astrophysics (mesa)",
                             "vizier online"]),
    ("Exoplanets",         ["exoplanet", "extra-solar planet", "extrasolar planet", "hot jupiter",
                            "super-earth", "rocky exoplanet", "rocky planet", "exoplanetary",
                            "transiting planet", "habitable zone", "transit spectroscopy",
                            "transmission spectroscopy", "exoatlas", "planet-star interaction",
                            "planetary system", "extra-solar planets"]),
    ("Geophysics",         ["mantle convection", "plate tectonics", "geodynamics", "earth mantle",
                            "seafloor", "subduction", "crustal strain", "geomagnetic",
                            "ionosphere", "auroral", "coronal mass ejection"]),
    ("High energy astrophysics", ["x-ray observation", "xmm-newton", "chandra observation",
                            " xrism ", "x-ray spectrum", "x-ray luminosity", "x-ray source",
                            "gamma-ray source", "x-ray transient", "x-ray burst spectrum",
                            "x-ray astronomy", "x-ray spectra", " heao ", "gamma-ray mission",
                            "cosmic ray", "galactic cosmic ray", "energy spectra of galactic"]),
    ("Galactic astronomy", ["milky way galaxy", "galactic disk", "galactic halo",
                            "sdss-iv manga", "manga survey", "galaxy: disk", "galaxy: halo",
                            "galaxy cluster", "galaxy: cluster"]),
]


def classify(title, abstract):
    text = " " + " ".join([title or "", abstract or ""]).lower() + " "
    assigned, seen = [], set()
    for list_name, phrases in RULES:
        if list_name in seen: continue
        for ph in phrases:
            if ph in text:
                assigned.append(list_name); seen.add(list_name); break
    return assigned or ["unassigned"]


# ---------------------------------------------------------------------------
# Title fingerprint for dedup
# ---------------------------------------------------------------------------

STOP = {"the","a","an","of","in","on","and","or","for","to","with","from","by","at","as",
        "is","are","be","this","that","its","we","our","using","use","via","new","i","ii","iii",
        "iv","v","their","some","has","been","not","but","about","into","than"}


def title_fp(title):
    s = unicodedata.normalize("NFKD", title or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^A-Za-z0-9 ]", " ", s).lower()
    return frozenset(w for w in s.split() if len(w) >= 3 and w not in STOP)


def fp_sim(a, b):
    if not a or not b: return 0.0
    return len(a & b) / len(a | b)


def norm_doi(s):
    s = (s or "").strip().lower()
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", s) or None


# ---------------------------------------------------------------------------
# Zotero DB connection (safe while Zotero is running)
# ---------------------------------------------------------------------------

def open_zotero():
    """Open Zotero's SQLite read-only. Tries immutable WAL mode first,
    then falls back to a temp copy of the .bak file."""
    for attempt in ("immutable", "bak"):
        try:
            if attempt == "immutable":
                conn = sqlite3.connect(
                    f"file:{ZOTERO_DB}?mode=ro&immutable=1", uri=True, timeout=3)
            else:
                if not os.path.exists(ZOTERO_BAK):
                    continue
                tmp = tempfile.mktemp(suffix=".sqlite")
                shutil.copy2(ZOTERO_BAK, tmp)
                conn = sqlite3.connect(tmp)
                conn._tmp = tmp   # stash for cleanup
            conn.row_factory = sqlite3.Row
            conn.execute("SELECT count(*) FROM items")   # test it
            return conn, attempt
        except sqlite3.OperationalError:
            continue
    raise RuntimeError(
        "Cannot open Zotero database. Close Zotero and retry, or ensure "
        "zotero.sqlite.bak exists.")


def close_zotero(conn):
    tmp = getattr(conn, "_tmp", None)
    conn.close()
    if tmp and os.path.exists(tmp):
        os.unlink(tmp)


# ---------------------------------------------------------------------------
# Query Zotero items
# ---------------------------------------------------------------------------

ITEM_QUERY = """
    SELECT
        i.itemID, i.key AS zkey, i.dateAdded, i.dateModified,
        MAX(CASE WHEN f.fieldName='title'            THEN dv.value END) AS title,
        MAX(CASE WHEN f.fieldName='abstractNote'     THEN dv.value END) AS abstract,
        MAX(CASE WHEN f.fieldName='date'             THEN dv.value END) AS date,
        MAX(CASE WHEN f.fieldName='publicationTitle' THEN dv.value END) AS journal,
        MAX(CASE WHEN f.fieldName='volume'           THEN dv.value END) AS volume,
        MAX(CASE WHEN f.fieldName='issue'            THEN dv.value END) AS issue,
        MAX(CASE WHEN f.fieldName='pages'            THEN dv.value END) AS pages,
        MAX(CASE WHEN f.fieldName='DOI'              THEN dv.value END) AS doi,
        MAX(CASE WHEN f.fieldName='url'              THEN dv.value END) AS url,
        MAX(CASE WHEN f.fieldName='citationKey'      THEN dv.value END) AS citekey,
        t.typeName
    FROM items i
    JOIN itemTypes t ON t.itemTypeID=i.itemTypeID
      AND t.typeName IN ({placeholders})
    LEFT JOIN itemData   id ON id.itemID=i.itemID
    LEFT JOIN itemDataValues dv ON dv.valueID=id.valueID
    LEFT JOIN fields     f  ON f.fieldID=id.fieldID
    WHERE i.itemID NOT IN (SELECT itemID FROM deletedItems)
    {since_clause}
    GROUP BY i.itemID
    ORDER BY i.dateModified DESC
"""


def query_zotero_items(zc, since=None):
    """Return list of dicts for all (or recently modified) Zotero papers."""
    placeholders = ",".join("?" * len(ZOTERO_TYPES))
    since_clause = "AND i.dateModified > ?" if since else ""
    params = list(ZOTERO_TYPES) + ([since] if since else [])
    rows = zc.execute(
        ITEM_QUERY.format(placeholders=placeholders, since_clause=since_clause),
        params).fetchall()

    items = []
    for r in rows:
        d = dict(r)
        if not d.get("title"):
            continue

        # Authors
        authors = zc.execute("""
            SELECT c.lastName, c.firstName, ic.orderIndex
            FROM itemCreators ic JOIN creators c ON c.creatorID=ic.creatorID
            WHERE ic.itemID=? ORDER BY ic.orderIndex
        """, (d["itemID"],)).fetchall()
        d["authors"] = " and ".join(
            f"{a['lastName']}, {a['firstName']}" if a["firstName"] else a["lastName"]
            for a in authors)

        # PDF attachment (first PDF in storage)
        att = zc.execute("""
            SELECT ai.key AS akey, a.path
            FROM itemAttachments a JOIN items ai ON ai.itemID=a.itemID
            WHERE a.parentItemID=? AND a.contentType='application/pdf'
              AND a.path LIKE 'storage:%'
              AND ai.itemID NOT IN (SELECT itemID FROM deletedItems)
            ORDER BY a.itemID DESC LIMIT 1
        """, (d["itemID"],)).fetchone()
        d["pdf_path"] = None
        if att:
            fname = att["path"][len("storage:"):]
            candidate = os.path.join(ZOTERO_DIR, "storage", att["akey"], fname)
            if os.path.isfile(candidate):
                d["pdf_path"] = candidate

        # arXiv ID
        doi = norm_doi(d.get("doi"))
        d["doi_norm"] = doi
        arxiv = None
        if doi and "arxiv" in doi:
            arxiv = re.sub(r"(?i)^10\.48550/arxiv\.", "", doi)
        url = d.get("url") or ""
        if not arxiv and "arxiv.org" in url:
            m = re.search(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", url)
            if m: arxiv = m.group(1)
        d["arxiv"] = arxiv

        # Year
        year = None
        for ys in [d.get("year"), (d.get("date") or "")[:4]]:
            try:
                y = int(ys or 0)
                if 1800 <= y <= 2100: year = y; break
            except (ValueError, TypeError): pass
        d["year"] = year

        items.append(d)
    return items


# ---------------------------------------------------------------------------
# papers-vela DB helpers
# ---------------------------------------------------------------------------

def build_dedup_maps(c):
    by_doi   = {r["doi"]: r["id"]
                for r in c.execute("SELECT id, lower(doi) AS doi FROM papers WHERE doi IS NOT NULL AND doi!=''")
                if r["doi"]}
    by_arxiv = {r["arxiv"]: r["id"]
                for r in c.execute("SELECT id, arxiv FROM papers WHERE arxiv IS NOT NULL AND arxiv!=''")
                if r["arxiv"]}
    by_ck    = {(r["citekey"] or "").lower(): r["id"]
                for r in c.execute("SELECT id, citekey FROM papers WHERE citekey IS NOT NULL AND citekey!=''")
                if r["citekey"]}
    lib_fps  = [(r["id"], title_fp(r["title"]))
                for r in c.execute("SELECT id, title FROM papers WHERE title IS NOT NULL AND title!=''")]
    return by_doi, by_arxiv, by_ck, lib_fps


def find_existing(item, by_doi, by_arxiv, by_ck, lib_fps):
    """Return paper id if item already in library, else None."""
    if item["doi_norm"] and by_doi.get(item["doi_norm"]):
        return by_doi[item["doi_norm"]], "doi"
    if item["arxiv"] and by_arxiv.get(item["arxiv"]):
        return by_arxiv[item["arxiv"]], "arxiv"
    if item.get("citekey") and by_ck.get((item["citekey"] or "").lower()):
        return by_ck[item["citekey"].lower()], "citekey"
    fp = title_fp(item["title"])
    for lid, lfp in lib_fps:
        if fp_sim(fp, lfp) >= 0.85:
            return lid, "title"
    return None, None


def get_or_create_list(c, name):
    row = c.execute("SELECT id FROM lists WHERE name=?", (name,)).fetchone()
    if row: return row["id"]
    return c.execute(
        "INSERT INTO lists(name,source,n_members) VALUES(?,'zotero',0)", (name,)).lastrowid


def insert_paper(c, item, by_doi, by_arxiv, by_ck, lib_fps,
                 touched_lists, dry_run=False, verbose=False):
    lists = classify(item["title"], item.get("abstract") or "")
    if dry_run:
        if verbose:
            print(f"    + {item['title'][:70]}  lists={lists}  pdf={'yes' if item['pdf_path'] else 'no'}")
        touched_lists.update(lists)
        return None

    cur = c.execute(
        "INSERT INTO papers(citekey,title,authors,year,journal,volume,issue,pages,"
        "doi,arxiv,abstract,source,in_cloud,pdf_on_disk,pdf_path) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,'zotero',0,?,?)",
        (item.get("citekey"), clean_text(item["title"]), item["authors"],
         item["year"], clean_text(item.get("journal") or ""),
         item.get("volume"), item.get("issue"), item.get("pages"),
         item["doi_norm"], item["arxiv"], clean_text(item.get("abstract") or ""),
         1 if item["pdf_path"] else 0, item["pdf_path"]))
    pid = cur.lastrowid

    if item["doi_norm"]: by_doi[item["doi_norm"]] = pid
    if item["arxiv"]:    by_arxiv[item["arxiv"]] = pid
    if item.get("citekey"): by_ck[(item["citekey"] or "").lower()] = pid
    lib_fps.append((pid, title_fp(item["title"])))

    for lname in lists:
        lid = get_or_create_list(c, lname)
        c.execute("INSERT OR IGNORE INTO paper_lists(paper_id,list_id) VALUES(?,?)", (pid, lid))
        touched_lists.add(lname)

    if verbose:
        print(f"    + {item['title'][:70]}  lists={lists}")
    return pid


def reconcile_existing(pa, existing_id, item, dry_run=False, verbose=False):
    """Bring a PA record up to date with Zotero's current state.

    Zotero is the source of truth. Rules:
      - pdf_path: always use Zotero's current attachment.
      - DOI: only upgrade arXiv DOI -> journal DOI, never downgrade.
      - All other fields: use Zotero's value whenever it is non-empty,
        regardless of what PA currently holds. Never overwrite with empty.
    Returns dict of fields that were (or would be) changed.
    """
    row = pa.execute(
        "SELECT pdf_path, doi, title, authors, abstract, year, "
        "journal, volume, issue, pages FROM papers WHERE id=?",
        (existing_id,)).fetchone()

    updates = {}

    # pdf_path — always sync to Zotero's current attachment
    z_pdf = item["pdf_path"]
    if z_pdf and z_pdf != (row["pdf_path"] or ""):
        updates["pdf_path"]    = z_pdf
        updates["pdf_on_disk"] = 1

    # DOI — only upgrade arXiv -> journal, never downgrade
    z_doi  = item.get("doi_norm") or ""
    pa_doi = norm_doi(row["doi"] or "") or ""
    if (z_doi and "10.48550" not in z_doi and re.match(r"^10\.\d{4}", z_doi)
            and "10.48550" in pa_doi):
        updates["doi"] = z_doi

    # All text fields: Zotero wins when it has a non-empty value
    for field, z_val in [
        ("title",    (item.get("title")    or "").strip()),
        ("authors",  (item.get("authors")  or "").strip()),
        ("abstract", (item.get("abstract") or "").strip()),
        ("journal",  (item.get("journal")  or "").strip()),
        ("volume",   (item.get("volume")   or "").strip()),
        ("issue",    (item.get("issue")    or "").strip()),
        ("pages",    (item.get("pages")    or "").strip()),
    ]:
        if z_val and z_val != (row[field] or "").strip():
            updates[field] = z_val

    # year — int comparison
    z_year = item.get("year")
    if z_year and z_year != row["year"]:
        updates["year"] = z_year

    if updates:
        if verbose:
            print(f"    ~ reconcile id={existing_id}: {', '.join(k for k in updates if k != 'pdf_on_disk')}")
        if not dry_run:
            cols = ", ".join(f"{k}=?" for k in updates)
            pa.execute(f"UPDATE papers SET {cols} WHERE id=?",
                       list(updates.values()) + [existing_id])

    return updates


# ---------------------------------------------------------------------------
# State persistence (tracks last sync time to avoid full re-scan)
# ---------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except Exception:
            pass
    return {}


def save_state(state):
    json.dump(state, open(STATE_FILE, "w"), indent=2)


# ---------------------------------------------------------------------------
# Index / bib freshness
# ---------------------------------------------------------------------------

def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def run_reindex(pa, lists=None):
    """Reindex the given lists (all if None). Returns count of lists updated."""
    n = reindex_lists(pa, list(lists) if lists else None)
    pa.commit()
    return n


def run_bib():
    """Regenerate references.bib + INDEX.md via generate_outputs.py."""
    gen = os.path.join(PAPERS_VELA, "generate_outputs.py")
    env = {**os.environ, "PAPERS_LIB": LIB}
    result = subprocess.run([sys.executable, gen], env=env,
                            capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[sync] bib generation warning: {result.stderr.strip()}")
    return result.returncode == 0


def check_staleness():
    """Report whether the list index and references.bib are behind the DB."""
    state   = load_state()
    bib     = os.path.join(LIB, "references.bib")
    db_mt   = os.path.getmtime(DB) if os.path.exists(DB) else 0
    bib_mt  = os.path.getmtime(bib) if os.path.exists(bib) else 0

    # Check list_summary freshness: compare updated_at to newest paper.added
    pa = sqlite3.connect(DB); pa.row_factory = sqlite3.Row
    newest_paper = pa.execute(
        "SELECT max(added) AS t FROM papers").fetchone()["t"] or ""
    oldest_index = pa.execute(
        "SELECT min(updated_at) AS t FROM list_summary").fetchone()["t"] or ""
    pa.close()

    index_stale = oldest_index < newest_paper[:10]   # date-level comparison
    bib_stale   = bib_mt < db_mt

    print("[check] Index staleness:")
    print(f"  newest paper added : {newest_paper}")
    print(f"  oldest list indexed: {oldest_index}  {'⚠ STALE' if index_stale else '✓ current'}")
    print(f"  references.bib     : {'⚠ STALE' if bib_stale else '✓ current'} "
          f"({'missing' if not os.path.exists(bib) else 'older than DB' if bib_stale else 'up to date'})")
    print(f"  last sync          : {state.get('last_sync_at', 'never')}")
    return index_stale, bib_stale


# ---------------------------------------------------------------------------
# Single sync pass
# ---------------------------------------------------------------------------

def sync_pass(dry_run=False, verbose=False, incremental=True, prune_orphans=False):
    state    = load_state()
    last_sync = state.get("last_zotero_modified") if incremental else None

    # Open Zotero
    try:
        zc, mode = open_zotero()
    except RuntimeError as e:
        print(f"[sync] ERROR: {e}"); return {}

    if verbose and mode == "bak":
        print("[sync] WARNING: using zotero.sqlite.bak — may miss very recent changes")

    items = query_zotero_items(zc, since=last_sync)
    if not items:
        print(f"[sync] nothing changed since last sync ({last_sync or 'ever'})")
        close_zotero(zc)
        return {}

    max_modified = max(i["dateModified"] for i in items)
    print(f"[sync] {len(items)} Zotero items to check (mode={mode}, since={last_sync or 'all'})")

    pa = sqlite3.connect(DB); pa.row_factory = sqlite3.Row
    by_doi, by_arxiv, by_ck, lib_fps = build_dedup_maps(pa)

    stats         = {"new": 0, "reconciled": 0, "already_present": 0, "orphans_pruned": 0}
    touched_lists = set()
    matched_pa_ids = set()

    for item in items:
        existing_id, match_reason = find_existing(item, by_doi, by_arxiv, by_ck, lib_fps)

        if existing_id is None:
            new_id = insert_paper(pa, item, by_doi, by_arxiv, by_ck, lib_fps,
                                  touched_lists, dry_run, verbose)
            if new_id:
                matched_pa_ids.add(new_id)
            stats["new"] += 1
        else:
            matched_pa_ids.add(existing_id)
            changed_fields = reconcile_existing(pa, existing_id, item, dry_run, verbose)
            if changed_fields:
                stats["reconciled"] += 1
                if not dry_run:
                    for r in pa.execute(
                        "SELECT l.name FROM lists l JOIN paper_lists pl "
                        "ON pl.list_id=l.id WHERE pl.paper_id=?", (existing_id,)):
                        touched_lists.add(r["name"])
            else:
                stats["already_present"] += 1

    # Orphan detection — only meaningful on a full scan
    if not incremental:
        all_ids = {r["id"] for r in pa.execute("SELECT id FROM papers")}
        orphan_ids = all_ids - matched_pa_ids
        for pid in sorted(orphan_ids):
            row = pa.execute(
                "SELECT citekey, doi FROM papers WHERE id=?", (pid,)).fetchone()
            # Skip records with no identifiers — they were likely never in Zotero
            if not (row["doi"] or "").strip():
                continue
            if prune_orphans and not dry_run:
                pa.execute("DELETE FROM papers WHERE id=?", (pid,))
                print(f"  ORPHAN pruned: [{row['citekey']}]")
                stats["orphans_pruned"] += 1
            else:
                print(f"  ORPHAN: [{row['citekey']}] doi={row['doi']} "
                      f"{'(--prune-orphans to remove)' if not prune_orphans else '(dry-run)'}")

    changed = stats["new"] + stats["reconciled"]

    if not dry_run:
        pa.commit()

        if changed:
            # Full scan → reindex everything; incremental → only affected lists
            n = run_reindex(pa, None if not incremental else (touched_lists if touched_lists else None))
            print(f"[sync] reindexed {n} lists")

            # Auto-regenerate references.bib + INDEX.md
            if run_bib():
                print(f"[sync] references.bib + INDEX.md regenerated")

        # Persist state
        state["last_zotero_modified"] = max_modified
        state["last_sync_at"]         = _now_iso()
        if changed:
            state["last_indexed_at"]  = _now_iso()
            state["last_bib_at"]      = _now_iso()
        save_state(state)

    pa.close()
    close_zotero(zc)

    print(f"[sync] done: {stats['new']} new, {stats['reconciled']} reconciled, "
          f"{stats['already_present']} unchanged"
          + (f", {stats['orphans_pruned']} orphans pruned" if stats["orphans_pruned"] else ""))
    return stats


# ---------------------------------------------------------------------------
# Push PA-only records → Zotero
# ---------------------------------------------------------------------------

_FLD = {"title":1,"date":6,"url":13,"volume":19,"pages":32,
        "publicationTitle":38,"DOI":59,"issue":76,"extra":16}
_JOURNAL_TYPE  = 22
_PREPRINT_TYPE = 31
_AUTHOR_TYPE   = 8   # creatorTypeID for 'author' in journalArticle/preprint
_KEY_CHARS     = string.ascii_letters + string.digits


def _zotero_running():
    return subprocess.run(["pgrep", "-ix", "zotero"],
                          capture_output=True).returncode == 0


def _new_zotero_key(existing):
    for _ in range(1000):
        k = "".join(random.choices(_KEY_CHARS, k=8))
        if k not in existing:
            existing.add(k)
            return k
    raise RuntimeError("Could not generate a unique Zotero key")


def _set_field(zc, iid, fid, val):
    if not val:
        return
    val = str(val)
    zc.execute("INSERT OR IGNORE INTO itemDataValues(value) VALUES(?)", (val,))
    vid = zc.execute("SELECT valueID FROM itemDataValues WHERE value=?",
                     (val,)).fetchone()[0]
    zc.execute("INSERT OR IGNORE INTO itemData(itemID,fieldID,valueID) VALUES(?,?,?)",
               (iid, fid, vid))


def _upsert_creator(zc, last, first):
    first = first or ""
    zc.execute("INSERT OR IGNORE INTO creators(firstName,lastName,fieldMode) VALUES(?,?,0)",
               (first, last))
    return zc.execute(
        "SELECT creatorID FROM creators WHERE lastName=? AND firstName=? AND fieldMode=0",
        (last, first)).fetchone()[0]


# An initials token. STRICT requires a period or a lone capital ('A', 'A.',
# 'Ch.', 'H.-W.', 'A.St.J.'). LOOSE also allows the period-less Hungarian forms
# ('Gy', 'Zs') — but that shape is indistinguishable from a name particle
# ('El', 'Den', 'Gay', 'The'), so LOOSE is only safe where a following real
# surname is also required (see _parse_authors).
_INITIALS_STRICT_RE = re.compile(r"^(?:[A-Z][a-z]{0,2}\.|[A-Z])(?:[-.]\s?[A-Z][a-z]{0,2}\.?)*$")
_INITIALS_RE        = re.compile(r"^(?:[A-Z][a-z]{0,2}\.?)(?:[-.]\s?[A-Z][a-z]{0,2}\.?)*$")
_SUFFIX_RE          = re.compile(r"^(?:Jr|Sr|II|III|IV)\.?$", re.I)


def _split_given_last(name, strict=False):
    """'H.-W. Becker' → ('Becker', 'H.-W.'); 'P. von Brentano' → ('von Brentano', 'P.')."""
    rx = _INITIALS_STRICT_RE if strict else _INITIALS_RE
    toks = name.split()
    i = 0
    # Never consume the final token: the surname is always at least one token,
    # so a short surname ('Oh', 'Tan') can't be mistaken for another initial.
    while i < len(toks) - 1 and rx.match(toks[i]):
        i += 1
    if i == 0:                            # no leading initials → not 'Initials Surname'
        return (name.strip(), "")
    return (" ".join(toks[i:]).strip(), " ".join(toks[:i]).strip())


def _parse_authors(s):
    """'Last, First and Last, First' → [(last, first), ...]

    Also handles comma-separated lists in initial-first order
    ('A. Spyrou, S. Harissopulos, P. Demetriou'), which some records carry.
    Without this, the whole list collapses into a single creator whose
    lastName is the first author and whose firstName is everyone else.
    """
    parts = []
    for a in (s or "").split(" and "):
        a = a.strip()
        if not a:
            continue

        segs = [x.strip() for x in a.split(",") if x.strip()]
        # Re-join a trailing suffix onto the name it belongs to ('Iben, I., Jr.').
        merged = []
        for seg in segs:
            if merged and _SUFFIX_RE.match(seg):
                merged[-1] += ", " + seg
            else:
                merged.append(seg)
        segs = merged

        # A comma-separated author list: every segment must read as 'Initials
        # Surname' *and* leave a surname that is not itself initials. Without
        # that second test, 'El Eid, M. F.' parses as the two-author list
        # ('Eid','El') + ('F.','M.') — the trailing 'M. F.' looks like a name
        # only because _split_given_last always reserves a final token.
        def _list_member(x):
            last, first = _split_given_last(x)
            # STRICT here: 'M. F.' is initials and disqualifies, but a short
            # period-less surname ('Oh', 'Tan', 'Li') must still qualify.
            return bool(first) and not _INITIALS_STRICT_RE.match(last)

        if len(segs) > 1 and all(_list_member(x) for x in segs):
            parts.extend(_split_given_last(x) for x in segs)
        elif "," in a:
            last, first = a.split(",", 1)
            parts.append((last.strip(), first.strip()))
        else:
            # No comma to anchor on: only split when the initials are
            # unambiguous, so 'The Galah Collaboration' stays one name.
            parts.append(_split_given_last(a, strict=True))
    return parts


def push_orphans_to_zotero(dry_run=False, verbose=False):
    """Push papers-vela records that have no Zotero counterpart into Zotero.

    Performs a full scan to detect orphans, then creates a Zotero journalArticle
    (or preprint) item for each. Requires Zotero to be CLOSED — writes directly
    to zotero.sqlite.

    After pushing, call sync_pass(incremental=False) to reconcile back so that
    papers-vela's records are linked to the new Zotero items.
    """
    zc = sqlite3.connect(ZOTERO_DB); zc.row_factory = sqlite3.Row
    pa = sqlite3.connect(DB);        pa.row_factory = sqlite3.Row

    # Full Zotero scan to find which PA ids are already matched
    zotero_items = query_zotero_items(zc)
    by_doi, by_arxiv, by_ck, lib_fps = build_dedup_maps(pa)
    matched = set()
    for item in zotero_items:
        pid, _ = find_existing(item, by_doi, by_arxiv, by_ck, lib_fps)
        if pid:
            matched.add(pid)

    all_ids = {r["id"] for r in pa.execute("SELECT id FROM papers")}
    orphan_ids = all_ids - matched

    orphans = []
    for pid in sorted(orphan_ids):
        row = pa.execute(
            "SELECT id,citekey,title,authors,year,journal,doi,arxiv,volume,issue,pages "
            "FROM papers WHERE id=?", (pid,)).fetchone()
        if row["doi"] or row["arxiv"]:
            orphans.append(row)

    print(f"[push-orphans] {len(orphans)} PA record(s) not in Zotero")
    if not orphans or dry_run:
        for row in orphans:
            print(f"  ~ [{row['citekey']}] {(row['title'] or '')[:60]}")
        zc.close(); pa.close()
        return

    lib_id = zc.execute("SELECT libraryID FROM libraries LIMIT 1").fetchone()[0]
    existing_keys = {r[0] for r in zc.execute("SELECT key FROM items")}
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pushed = 0

    for row in orphans:
        doi = (row["doi"] or "").strip()
        is_arxiv = "10.48550" in doi.lower()
        item_type = _PREPRINT_TYPE if is_arxiv else _JOURNAL_TYPE

        key = _new_zotero_key(existing_keys)
        cur = zc.execute("""
            INSERT INTO items(itemTypeID,dateAdded,dateModified,clientDateModified,
                              libraryID,key,version,synced)
            VALUES(?,?,?,?,?,?,0,0)
        """, (item_type, now, now, now, lib_id, key))
        iid = cur.lastrowid

        _set_field(zc, iid, _FLD["title"],            row["title"])
        _set_field(zc, iid, _FLD["DOI"],              doi or None)
        _set_field(zc, iid, _FLD["publicationTitle"], row["journal"])
        _set_field(zc, iid, _FLD["volume"],           row["volume"])
        _set_field(zc, iid, _FLD["issue"],            row["issue"])
        _set_field(zc, iid, _FLD["pages"],            row["pages"])
        _set_field(zc, iid, _FLD["date"],             str(row["year"]) if row["year"] else None)
        if row["arxiv"]:
            _set_field(zc, iid, _FLD["url"], f"https://arxiv.org/abs/{row['arxiv']}")
        if row["citekey"]:
            _set_field(zc, iid, _FLD["extra"], f"Citation Key: {row['citekey']}")

        for idx, (last, first) in enumerate(_parse_authors(row["authors"])):
            cid = _upsert_creator(zc, last, first)
            zc.execute(
                "INSERT INTO itemCreators(itemID,creatorID,creatorTypeID,orderIndex) VALUES(?,?,?,?)",
                (iid, cid, _AUTHOR_TYPE, idx))

        if verbose:
            print(f"  + [{row['citekey']}] {(row['title'] or '')[:60]}  key={key}")
        pushed += 1

    zc.commit()
    zc.close(); pa.close()
    print(f"[push-orphans] created {pushed} Zotero item(s)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once",     action="store_true", help="one pass then exit (default)")
    ap.add_argument("--watch",    action="store_true", help="loop, re-sync on Zotero DB changes")
    ap.add_argument("--interval", type=int, default=60,
                    help="poll interval in seconds for --watch (default 60)")
    ap.add_argument("--full",     action="store_true",
                    help="ignore last-sync state; scan all Zotero items")
    ap.add_argument("--check",    action="store_true",
                    help="report index/bib staleness without syncing")
    ap.add_argument("--reindex",  action="store_true",
                    help="force-reindex all lists and regenerate bib now")
    ap.add_argument("--fetch-pdfs", action="store_true",
                    help="after syncing, download missing PDFs into Zotero storage "
                         "(requires Zotero to be closed)")
    ap.add_argument("--push-orphans", action="store_true",
                    help="push PA-only records into Zotero as new items "
                         "(requires Zotero closed; runs a full reconcile after)")
    ap.add_argument("--prune-orphans", action="store_true",
                    help="delete PA records with no matching Zotero item (full scan only)")
    ap.add_argument("--dry-run",  action="store_true")
    ap.add_argument("--verbose",  action="store_true")
    a = ap.parse_args()
    require_lib()

    if not os.path.exists(DB):
        sys.exit(f"papers-vela library not found at {DB}\n"
                 f"Set PAPERS_LIB or run zotero_import.py first.")

    if a.push_orphans:
        if _zotero_running():
            sys.exit("[push-orphans] Zotero is running — close it first, then retry.")
        push_orphans_to_zotero(dry_run=a.dry_run, verbose=a.verbose)
        if not a.dry_run:
            print("[push-orphans] reconciling back into papers-vela…")
            sync_pass(dry_run=False, verbose=a.verbose, incremental=False)
        return

    if a.check:
        index_stale, bib_stale = check_staleness()
        if index_stale or bib_stale:
            print("[check] run with --reindex to fix")
        return

    if a.reindex:
        pa = sqlite3.connect(DB); pa.row_factory = sqlite3.Row
        n = run_reindex(pa)
        print(f"[reindex] {n} lists updated")
        pa.close()
        if run_bib():
            print("[reindex] references.bib + INDEX.md regenerated")
        state = load_state()
        state["last_indexed_at"] = _now_iso()
        state["last_bib_at"]     = _now_iso()
        save_state(state)
        return

    if a.watch:
        print(f"[sync] watching Zotero DB every {a.interval}s (Ctrl-C to stop)")
        last_mtime = 0
        while True:
            try:
                mtime = os.path.getmtime(ZOTERO_DB)
            except OSError:
                mtime = 0
            if mtime != last_mtime:
                last_mtime = mtime
                sync_pass(dry_run=a.dry_run, verbose=a.verbose,
                          incremental=not a.full, prune_orphans=a.prune_orphans)
            time.sleep(a.interval)
    else:
        sync_pass(dry_run=a.dry_run, verbose=a.verbose, incremental=not a.full,
                  prune_orphans=a.prune_orphans)

    # Optional: download missing PDFs into Zotero storage after sync
    if a.fetch_pdfs:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from fetch_pdfs import fetch_missing_pdfs, zotero_is_running
        if zotero_is_running():
            print("\n[fetch-pdfs] ⚠  Zotero is running — PDF fetch skipped.\n"
                  "             Close Zotero and re-run with --fetch-pdfs.")
        else:
            print("\n[fetch-pdfs] downloading missing PDFs into Zotero storage...")
            fetch_missing_pdfs(dry_run=a.dry_run, verbose=a.verbose)


if __name__ == "__main__":
    main()
