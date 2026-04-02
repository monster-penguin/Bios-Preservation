"""
bios_stage.py — Script 3 (stage)

Cross-references bios_database.sqlar with combined_platform_build.json and
stages a complete BIOS directory (or zip archive) for each selected platform.
"""

from __future__ import annotations

import configparser
import json
import os
import sqlite3
import sys
import zipfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLATFORMS = [
    "retrodeck", "retropie", "batocera", "emudeck",
    "recalbox", "retrobat", "lakka", "retroarch",
    "romm", "bizhawk",
]

PLATFORM_DISPLAY = {
    "retrodeck": "RetroDeck",
    "retropie":  "RetroPie",
    "batocera":  "Batocera",
    "emudeck":   "EmuDeck",
    "recalbox":  "Recalbox",
    "retrobat":  "Retrobat",
    "lakka":     "Lakka",
    "retroarch": "RetroArch",
    "romm":      "RomM",
    "bizhawk":   "BizHawk",
}

# Default platform selection used when no config entry is present.
# (retroarch/batocera/lakka/recalbox/retrodeck/retropie on, emudeck/retrobat off)
_STAGE_DEFAULTS: dict[str, bool] = {
    "retrodeck": True,
    "retropie":  True,
    "batocera":  True,
    "emudeck":   False,
    "recalbox":  True,
    "retrobat":  False,
    "lakka":     True,
    "retroarch": True,
}


# ---------------------------------------------------------------------------
# Platform selection prompt
# ---------------------------------------------------------------------------

def _confirm_platforms(config: configparser.ConfigParser) -> list[str]:
    """
    Show the current platform defaults and let the user toggle them per-run.
    Selections are NOT saved to the config file — they apply to this run only.
    Returns the list of platform names to process.
    """
    # Build current state: prefer config values, fall back to built-in defaults.
    current: dict[str, bool] = {}
    for p in PLATFORMS:
        raw = config.get("stage", p, fallback=None)
        if raw is not None:
            current[p] = raw.strip().lower() in ("yes", "true", "1")
        else:
            current[p] = _STAGE_DEFAULTS[p]

    while True:
        print(f"\n{'='*60}")
        print("  STAGE — Platform Selection")
        print("─" * 60)
        print("  Toggle platforms for this run (defaults shown):\n")
        for i, p in enumerate(PLATFORMS, start=1):
            tick = "YES" if current[p] else "no "
            print(f"  [{i}] [{tick}]  {PLATFORM_DISPLAY[p]}")
        print()
        print("  Enter a number to toggle  |  [A] all  |  [N] none  |  [C] continue")
        print("─" * 60)

        raw = input("  Choice: ").strip().upper()

        if raw == "C":
            break
        elif raw == "A":
            current = {p: True for p in PLATFORMS}
        elif raw == "N":
            current = {p: False for p in PLATFORMS}
        elif raw.isdigit() and 1 <= int(raw) <= len(PLATFORMS):
            p = PLATFORMS[int(raw) - 1]
            current[p] = not current[p]
        else:
            print("  Invalid input — enter a number, A, N, or C.")

    enabled = [p for p in PLATFORMS if current[p]]
    if enabled:
        print(f"\n  Staging: {', '.join(PLATFORM_DISPLAY[p] for p in enabled)}")
    else:
        print("\n  No platforms selected.")
    return enabled


# ---------------------------------------------------------------------------
# Path helper
# ---------------------------------------------------------------------------

def _resolve(path: str, base_dir: str) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = Path(base_dir) / p
    return str(p)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _best_sqlar_name(
    conn: sqlite3.Connection,
    canonical: str,
    declared_md5s: list[str] | None = None,
) -> tuple[str | None, str | None]:
    """
    Return (sqlar_name, status) for the best available copy of *canonical*.

    Selection priority:
    1. A verified variant whose MD5 matches one the platform declares
    2. Any verified variant (first found) — used when platform declares no MD5
       or no declared MD5 matches a stored variant
    3. Best non-verified variant (unverifiable, then mismatch_accepted)
    """
    # Try to find a verified variant matching a platform-declared MD5
    if declared_md5s:
        for md5 in declared_md5s:
            row = conn.execute(
                "SELECT sqlar_name, status FROM files "
                "WHERE canonical_name = ? AND md5 = ? AND status = 'verified'",
                (canonical, md5.lower()),
            ).fetchone()
            if row:
                return row[0], row[1]

    # Fall back to best available by status rank
    row = conn.execute(
        "SELECT sqlar_name, status FROM files "
        "WHERE canonical_name = ? "
        "ORDER BY CASE status "
        "  WHEN 'verified'          THEN 1 "
        "  WHEN 'unverifiable'      THEN 2 "
        "  WHEN 'mismatch_accepted' THEN 3 "
        "  ELSE 4 END LIMIT 1",
        (canonical,),
    ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def _get_blob(conn: sqlite3.Connection, sqlar_name: str) -> bytes | None:
    row = conn.execute("SELECT data FROM sqlar WHERE name = ?", (sqlar_name,)).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _write_file(full_path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "wb") as fh:
        fh.write(data)


# ---------------------------------------------------------------------------
# Per-platform staging
# ---------------------------------------------------------------------------

def stage_platform(
    conn: sqlite3.Connection,
    manifest: dict,
    platform_name: str,
    stage_dir: str,
    output_format: str,
) -> dict:
    platform_meta  = (manifest.get("platform_metadata") or {}).get(platform_name) or {}
    base_destination: str = platform_meta.get("base_destination") or ""

    summary: dict = {"staged": [], "warnings": [], "missing": []}

    # Prepare zip file if needed
    zf: zipfile.ZipFile | None = None
    if output_format == "zip":
        zip_path = os.path.join(stage_dir, f"{platform_name}_bios.zip")
        os.makedirs(stage_dir, exist_ok=True)
        zf = zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED)

    try:
        for canonical, fdata in manifest["files"].items():
            pinfo = (fdata.get("platforms") or {}).get(platform_name) or {}
            if not pinfo.get("known_file"):
                continue

            staging_paths: list[str] = pinfo.get("staging_paths") or [canonical]
            declared_md5s: list[str] = (pinfo.get("expected_hashes") or {}).get("md5") or []

            sqlar_name, status = _best_sqlar_name(conn, canonical, declared_md5s)
            if sqlar_name is None:
                summary["missing"].append(canonical)
                continue

            data = _get_blob(conn, sqlar_name)
            if data is None:
                summary["missing"].append(canonical)
                continue

            if status == "mismatch_accepted":
                summary["warnings"].append(
                    f"  WARN [{platform_name}] {canonical!r}: "
                    f"mismatch_accepted (hash unverified) — staging anyway"
                )

            for destination in staging_paths:
                # Construct relative path: base_destination / destination
                if base_destination and base_destination not in (".", ""):
                    rel = "/".join([base_destination.rstrip("/"), destination.lstrip("/")])
                else:
                    rel = destination
                rel = rel.replace("\\", "/")

                if output_format == "zip" and zf is not None:
                    zf.writestr(rel, data, compress_type=zipfile.ZIP_STORED)
                else:
                    full_path = os.path.join(
                        stage_dir, platform_name, rel.replace("/", os.sep)
                    )
                    _write_file(full_path, data)

                summary["staged"].append(rel)
    finally:
        if zf is not None:
            zf.close()

    return summary


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(config: configparser.ConfigParser, base_dir: str = ".") -> bool:
    section = "stage"

    sqlar_input    = _resolve(
        config.get(section, "sqlar_input", fallback="build/bios_database.sqlar"),
        base_dir,
    )
    manifest_input = _resolve(
        config.get(section, "manifest_input",
                   fallback="build/combined_platform_build.json"),
        base_dir,
    )
    stage_dir = _resolve(
        config.get(section, "stage_dir", fallback="stage"),
        base_dir,
    )
    output_format = config.get(section, "output_format", fallback="directory").strip().lower()

    if output_format not in ("directory", "zip"):
        print(f"[stage] ERROR: output_format must be 'directory' or 'zip', got {output_format!r}")
        return False

    # Which platforms to stage — confirmed interactively per run
    enabled: list[str] = _confirm_platforms(config)

    if not enabled:
        print("[stage] WARNING: no platforms enabled.  Set <platform> = yes in [stage].")
        return True

    # ── Load inputs ────────────────────────────────────────────────────────
    if not os.path.exists(manifest_input):
        print(f"[stage] ERROR: build manifest not found: {manifest_input!r}")
        return False
    if not os.path.exists(sqlar_input):
        print(f"[stage] ERROR: sqlar database not found: {sqlar_input!r}")
        return False

    with open(manifest_input, "r", encoding="utf-8") as fh:
        manifest: dict = json.load(fh)

    conn = sqlite3.connect(sqlar_input)

    # ── Stage each enabled platform ────────────────────────────────────────
    print(f"[stage] Staging {len(enabled)} platform(s) → {stage_dir!r}  "
          f"(format={output_format})")
    os.makedirs(stage_dir, exist_ok=True)

    grand_staged = grand_missing = grand_warnings = 0

    for p in enabled:
        print(f"\n  ── {p} ──────────────────────────────────────")
        summary = stage_platform(conn, manifest, p, stage_dir, output_format)

        for w in summary["warnings"]:
            print(w)

        n_staged  = len(summary["staged"])
        n_missing = len(summary["missing"])
        n_warn    = len(summary["warnings"])
        grand_staged   += n_staged
        grand_missing  += n_missing
        grand_warnings += n_warn

        if output_format == "zip":
            dest = os.path.join(stage_dir, f"{p}_bios.zip")
        else:
            dest = os.path.join(stage_dir, p)

        print(f"  Staged : {n_staged}")
        print(f"  Missing: {n_missing}")
        if n_warn:
            print(f"  Warnings (mismatch_accepted): {n_warn}")
        print(f"  Output : {dest!r}")

        if summary["missing"]:
            print("  Missing files:")
            for mf in summary["missing"]:
                print(f"    - {mf}")

    conn.close()
    print(f"\n[stage] Done.  "
          f"total staged={grand_staged}  missing={grand_missing}  "
          f"warnings={grand_warnings}")
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
        print(f"[launcher] Using user configuration: {user_conf}")
    sys.exit(0 if run(cfg, base_dir) else 1)
