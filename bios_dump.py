"""
bios_dump.py — Script 6 (dump)

Exports every file stored in bios_database.sqlar into a dated zip archive
in the dump/ folder.  Files are written into status-named subfolders:

  verified/               — stored as {md5}.{ext}  (hash IS the identity)
  unverifiable/           — stored at full staging path, e.g. np2kai/bios.rom
  mismatch_accepted/      — stored at full staging path, e.g. keropi/bios.rom

Using the full staging path for unverifiable and mismatch files prevents
filename collisions between files that share a basename across different
systems (e.g. np2kai/bios.rom vs keropi/bios.rom).

On re-ingest, the build scanner strips all path components automatically
(member_name = info.filename.split("/")[-1]), so filename matching works
correctly for all three status types.

e.g.  dump/20_mar_2026_dump.zip

Can be run standalone or called from the master launcher.
"""

from __future__ import annotations

import configparser
import json
import os
import sqlite3
import sys
import zipfile
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Path helper
# ---------------------------------------------------------------------------

def _resolve(path: str, base_dir: str) -> str:
    p = Path(path)
    return str(p if p.is_absolute() else Path(base_dir) / p)


# ---------------------------------------------------------------------------
# Date stamp
# ---------------------------------------------------------------------------

def _datestamp() -> str:
    """Return a date string in the format  20_mar_2026."""
    now = datetime.now()
    return f"{now.day}_{now.strftime('%b').lower()}_{now.strftime('%Y')}"


# ---------------------------------------------------------------------------
# Display name resolution
# ---------------------------------------------------------------------------

PLATFORMS = [
    "retrodeck", "retropie", "batocera", "emudeck",
    "recalbox", "retrobat", "lakka", "retroarch",
]


def _build_staging_path_map(build_manifest_path: str) -> dict[str, str]:
    """
    Return a mapping of lowercase canonical_name -> first staging path declared
    by any platform (e.g. "np2kai/bios.rom").  Used as the archive sub-path under
    the status subfolder, so files that share a basename across different systems
    (e.g. np2kai/bios.rom vs keropi/bios.rom) don't collide in the dump zip or
    when extracted to a directory.

    The build scanner's existing  member_name = info.filename.split("/")[-1]
    strips all path components, so re-ingest still matches on the bare filename.

    Falls back gracefully if the manifest is missing or malformed.
    """
    if not os.path.exists(build_manifest_path):
        return {}
    try:
        with open(build_manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except Exception:
        return {}

    staging_map: dict[str, str] = {}
    for canonical, fdata in (manifest.get("files") or {}).items():
        for p in PLATFORMS:
            pinfo = (fdata.get("platforms") or {}).get(p) or {}
            staging = pinfo.get("staging_paths") or []
            if staging and staging[0]:
                staging_map[canonical] = staging[0]  # e.g. "np2kai/bios.rom"
                break
    return staging_map


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(config: configparser.ConfigParser, base_dir: str = ".") -> bool:
    section = "dump"

    sqlar_input = _resolve(
        config.get("build", "sqlar_output", fallback="build/bios_database.sqlar"),
        base_dir,
    )
    build_manifest_path = _resolve(
        config.get("build", "json_output", fallback="build/combined_platform_build.json"),
        base_dir,
    )
    dump_dir = _resolve(
        config.get(section, "dump_dir", fallback="dump"),
        base_dir,
    )

    print(f"\n{'='*60}")
    print(f"  STEP: DUMP")
    print(f"{'='*60}")

    if not os.path.exists(sqlar_input):
        print(f"[dump] ERROR: database not found: {sqlar_input!r}")
        print("[dump] Run 'build' first to create the database.")
        return False

    os.makedirs(dump_dir, exist_ok=True)

    stamp   = _datestamp()
    counter = 1
    while True:
        suffix   = "" if counter == 1 else f"({counter})"
        zip_name = f"{stamp}_dump{suffix}.zip"
        zip_path = os.path.join(dump_dir, zip_name)
        if not os.path.exists(zip_path):
            break
        counter += 1

    print(f"[dump] Source        : {sqlar_input!r}")
    print(f"[dump] Build manifest: {build_manifest_path!r}")
    print(f"[dump] Dest          : {zip_path!r}")

    # Load staging path map for unverifiable / mismatch files
    staging_map = _build_staging_path_map(build_manifest_path)
    if staging_map:
        print(f"[dump] Loaded {len(staging_map)} staging paths from build manifest.")
    else:
        print("[dump] WARNING: build manifest not found or empty — "
              "unverifiable/mismatch files will use lowercase canonical names.")

    conn = sqlite3.connect(sqlar_input)
    try:
        # Join sqlar blobs with their status from the files table
        file_rows = conn.execute(
            "SELECT s.name, s.data, f.canonical_name, f.status "
            "FROM sqlar s "
            "JOIN files f ON f.sqlar_name = s.name "
            "ORDER BY f.status, s.name"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        print(f"[dump] ERROR: could not read database: {exc}")
        conn.close()
        return False
    conn.close()

    if not file_rows:
        print("[dump] WARNING: database is empty — nothing to dump.")
        return True

    # Status -> subfolder name
    SUBFOLDER = {
        "verified":          "verified",
        "unverifiable":      "unverifiable",
        "mismatch_accepted": "mismatch_accepted",
    }

    counts: dict[str, int] = {k: 0 for k in SUBFOLDER}
    skipped = 0

    try:
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
            for sqlar_name, data, canonical_name, status in file_rows:
                if data is None:
                    print(f"  WARN: {sqlar_name!r} has no data blob — skipping.")
                    skipped += 1
                    continue

                subfolder = SUBFOLDER.get(status, status)

                if status == "verified":
                    # Identity is the hash — keep {md5}.{ext} name
                    arc_path = f"verified/{sqlar_name}"
                else:
                    # Use the full staging path (e.g. "np2kai/bios.rom") so files that
                    # share a basename across different systems don't collide.
                    # The build scanner's split("/")[-1] strips all path components on
                    # re-ingest, so filename matching still works correctly.
                    staging_path = staging_map.get(canonical_name, canonical_name)
                    arc_path = f"{subfolder}/{staging_path}"
                zf.writestr(arc_path, data)
                counts[status] = counts.get(status, 0) + 1

    except OSError as exc:
        print(f"[dump] ERROR: could not write zip: {exc}")
        return False

    total   = sum(counts.values())
    size_mb = os.path.getsize(zip_path) / (1024 * 1024)
    print(f"[dump] Done.  "
          f"verified={counts.get('verified', 0)}  "
          f"unverifiable={counts.get('unverifiable', 0)}  "
          f"mismatch_accepted={counts.get('mismatch_accepted', 0)}  "
          f"skipped={skipped}  "
          f"total={total}  "
          f"{size_mb:.2f} MB.")
    return True


# ---------------------------------------------------------------------------
# Stand-alone execution
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    script_dir = Path(__file__).resolve().parent
    base_dir   = str(script_dir.parent)
    conf_path  = script_dir.parent / "configure" / "bios_preservation.conf"
    if not conf_path.exists():
        print(f"ERROR: {conf_path} not found")
        sys.exit(1)
    cfg = configparser.ConfigParser()
    cfg.read(str(conf_path), encoding="utf-8")
    user_conf = script_dir.parent / "configure" / "bios_preservation_user.conf"
    if user_conf.exists():
        cfg.read(str(user_conf), encoding="utf-8")
    sys.exit(0 if run(cfg, base_dir) else 1)
