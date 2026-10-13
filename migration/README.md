# migration/ — reference importers (ReadCube / Papers 3)

These are the scripts used to migrate **one specific** library off the ReadCube/Papers app
(local Papers 3 SQLite DB + per-list cloud BibTeX exports + emailed PDF zips). They contain
**hardcoded, user-specific paths** (e.g. an external scratch volume, a specific email/library
location) and are provided **as worked examples**, not as a general-purpose importer.

**New users do not need these.** The universal on-ramp is `../import_bib.py`: export your library
as BibTeX from whatever you use now (Mendeley, Zotero, ReadCube, EndCube, EndNote …) and import it.

Pipeline these illustrate (ReadCube → local library):
`extract_papers3.py` (old Papers 3 DB → library.db) → `ingest_cloud_bibs.py` (merge per-list
cloud BibTeX, set current list membership) → `copy_pdfs.py` / `harvest_pdf_zips.py` +
`match_zip_pdfs.py` + `place_gaps.py` (collect & match PDFs) → `finalize_library.py` (homogenize
names + by-list symlinks) → `backfill_ads.py` (abstracts + citation counts) → `../generate_outputs.py`.

Adapt the paths at the top of each file before running.
