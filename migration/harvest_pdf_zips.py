#!/usr/bin/env python3
"""
Harvest ReadCube 'PDF Export is ready' emails -> download each list's PDF zip to
FPOD19 scratch (internal disk is tight). Decodes the Microsoft safelink + quoted-
printable to the direct S3 url.

  --dry   : just list (list name, url found?, existing size) -- no download
  (default): download any zip not already present, into FPOD19/papers_scratch/zips/
"""
import subprocess, re, quopri, urllib.parse, os, sys

SCRATCH = "/Volumes/FPOD19/papers_scratch"
ZIPS = os.path.join(SCRATCH, "zips")
DRY = "--dry" in sys.argv

APPLESCRIPT = '''
tell application "Mail"
  set out to ""
  set msgs to (messages of inbox whose subject starts with "Your PDF Export is ready")
  repeat with m in msgs
    set out to out & "<<<MSG>>>" & linefeed & "SUBJECT::" & (subject of m) & linefeed & "<<<SRC>>>" & linefeed & (source of m) & linefeed
  end repeat
  return out
end tell
'''

S3RE = re.compile(r"https://s3\.amazonaws\.com/sync\.objects\.readcube\.com/exports/[^\s\"'<>&]+\.zip")


def sanitize(name):
    return name.replace("/", "_").strip()


def main():
    os.makedirs(ZIPS, exist_ok=True)
    res = subprocess.run(["osascript", "-e", APPLESCRIPT], capture_output=True, text=True)
    if res.returncode != 0:
        sys.exit("osascript failed: " + res.stderr)
    blocks = [b for b in res.stdout.split("<<<MSG>>>") if "SUBJECT::" in b]
    print(f"PDF-export emails found: {len(blocks)}")
    rows = []
    for b in blocks:
        subj = re.search(r"SUBJECT::(.*)", b)
        subj = subj.group(1).strip() if subj else ""
        lm = re.search(r'ready:\s*"([^"]+)"', subj)
        name = lm.group(1).strip() if lm else subj[:40]
        src = b.split("<<<SRC>>>", 1)[1] if "<<<SRC>>>" in b else ""
        dec = quopri.decodestring(src.encode("utf-8", "replace")).decode("utf-8", "replace")
        unq = urllib.parse.unquote(dec)
        m = S3RE.search(unq)
        rows.append((name, m.group(0) if m else None))

    ok = sum(1 for _, u in rows if u)
    print(f"links decoded: {ok}/{len(rows)}")
    downloaded = skipped = failed = 0
    for name, url in sorted(rows):
        dest = os.path.join(ZIPS, sanitize(name) + ".zip")
        have = os.path.exists(dest) and os.path.getsize(dest) > 0
        if DRY:
            sz = f"{os.path.getsize(dest)/1e6:.1f}MB" if have else "-"
            print(f"   {'OK ' if url else 'NO-URL'} {name:<34} existing:{sz}")
            continue
        if not url:
            failed += 1; print(f"   NO-URL  {name}"); continue
        if have:
            skipped += 1; continue
        r = subprocess.run(["curl", "-sSL", "-A", "Mozilla/5.0", "-o", dest, url])
        if r.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest) > 0:
            downloaded += 1
            print(f"   downloaded {name}  ({os.path.getsize(dest)/1e6:.1f} MB)")
        else:
            failed += 1; print(f"   FAILED {name}")
    if not DRY:
        total = sum(os.path.getsize(os.path.join(ZIPS, f)) for f in os.listdir(ZIPS))
        print(f"\nDONE: downloaded={downloaded}, skipped(existing)={skipped}, failed={failed}")
        print(f"zips dir: {ZIPS}  total={total/1e9:.2f} GB")


if __name__ == "__main__":
    main()
