"""
bios_backup.py — Script 5 (backup)

Copies bios_database.sqlar into the backup/ folder, named by today's date.
e.g.  backup/20_mar_2026_backup.sqlar

Can be run standalone or called from the master launcher.
"""

from __future__ import annotations

import configparser
import os
import shutil
import sys
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
# Entry point
# ---------------------------------------------------------------------------

def run(config: configparser.ConfigParser, base_dir: str = ".") -> bool:
    section = "backup"

    sqlar_input = _resolve(
        config.get("build", "sqlar_output", fallback="build/bios_database.sqlar"),
        base_dir,
    )
    backup_dir = _resolve(
        config.get(section, "backup_dir", fallback="backup"),
        base_dir,
    )

    print(f"\n{'='*60}")
    print(f"  STEP: BACKUP")
    print(f"{'='*60}")

    if not os.path.exists(sqlar_input):
        print(f"[backup] ERROR: database not found: {sqlar_input!r}")
        print("[backup] Run 'build' first to create the database.")
        return False

    os.makedirs(backup_dir, exist_ok=True)

    stamp    = _datestamp()
    counter  = 1
    while True:
        suffix    = "" if counter == 1 else f"({counter})"
        dest_name = f"{stamp}_backup{suffix}.sqlar"
        dest_path = os.path.join(backup_dir, dest_name)
        if not os.path.exists(dest_path):
            break
        counter += 1

    print(f"[backup] Source : {sqlar_input!r}")
    print(f"[backup] Dest   : {dest_path!r}")

    try:
        shutil.copy2(sqlar_input, dest_path)
    except OSError as exc:
        print(f"[backup] ERROR: copy failed: {exc}")
        return False

    size_mb = os.path.getsize(dest_path) / (1024 * 1024)
    print(f"[backup] Done.  {size_mb:.2f} MB written.")
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
