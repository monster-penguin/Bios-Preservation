"""
bios_configure.py — Script 7 (configure)

Interactive questionnaire that walks the user through every key setting in
bios_preservation.conf.  Writes the chosen values to:

    configure/bios_preservation_user.conf

The master launcher automatically uses this file (overlaid on the default
conf) when it exists.

First question: edit configuration  OR  restore defaults (deletes user.conf).
Each subsequent question shows the current/default value; the user presses
Enter (or Y) to accept, or types a new value.
At the end a summary of changes is shown and the user must confirm before
the file is written.
"""

from __future__ import annotations

import configparser
import os
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Defaults  (mirrors bios_preservation.conf)
# ---------------------------------------------------------------------------

DEFAULTS: dict[str, dict[str, str]] = {
    "update": {
        "yaml_source":    "url",
        "yaml_url_base":  "https://raw.githubusercontent.com/Abdess/retrobios/main/platforms/",
        "yaml_local_dir": "yaml",
        "yaml_cache_dir": "yaml",
        "yaml_refresh":   "false",
        "output_dir":     "update",
    },
    "build": {
        "manifest_input": "update/combined_platform_manifest.json",
        "sqlar_output":   "build/bios_database.sqlar",
        "json_output":    "build/combined_platform_build.json",
        "csv_output":     "build/combined_platform_build.csv",
        "incremental":    "true",
    },
    "stage": {
        "sqlar_input":    "build/bios_database.sqlar",
        "manifest_input": "build/combined_platform_build.json",
        "stage_dir":      "stage",
        "output_format":  "directory",
    },
    "report": {
        "sqlar_input":    "build/bios_database.sqlar",
        "manifest_input": "build/combined_platform_build.json",
        "report_dir":     "report",
    },
    "backup": {
        "backup_dir": "backup",
    },
    "dump": {
        "dump_dir": "dump",
    },
}

# Human-readable descriptions for each key
DESCRIPTIONS: dict[str, dict[str, str]] = {
    "update": {
        "yaml_source":    "YAML source — 'url' to download from GitHub, 'local' to use local files",
        "yaml_url_base":  "Base URL for downloading platform YAMLs",
        "yaml_local_dir": "Local directory for YAML files (used when yaml_source = local)",
        "yaml_cache_dir": "Cache directory for downloaded YAMLs",
        "yaml_refresh":   "Always re-download YAMLs even if cached? (true/false)",
        "output_dir":     "Directory where the combined manifest is written",
    },
    "build": {
        "manifest_input": "Path to the update manifest (input)",
        "sqlar_output":   "Path for the sqlar database (output)",
        "json_output":    "Path for the build JSON manifest (output)",
        "csv_output":     "Path for the build CSV manifest (output)",
        "incremental":    "Incremental build — preserve previously found files? (true/false)",
    },
    "stage": {
        "sqlar_input":    "Path to the sqlar database (input)",
        "manifest_input": "Path to the build manifest (input)",
        "stage_dir":      "Root directory for staged platform output",
        "output_format":  "Output format — 'directory' or 'zip'",
    },
    "report": {
        "sqlar_input":    "Path to the sqlar database (input)",
        "manifest_input": "Path to the build manifest (input)",
        "report_dir":     "Directory where per-platform report CSVs are written",
    },
    "backup": {
        "backup_dir": "Directory where backup .sqlar files are written",
    },
    "dump": {
        "dump_dir": "Directory where dump .zip files are written",
    },
}

SECTION_LABELS = {
    "update": "STEP 1 — UPDATE  (parse platform YAMLs)",
    "build":  "STEP 2 — BUILD   (scan sources, build database)",
    "stage":  "STEP 3 — STAGE   (stage files per platform)",
    "report": "STEP 4 — REPORT  (generate per-platform reports)",
    "backup": "STEP 5 — BACKUP",
    "dump":   "STEP 6 — DUMP",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _divider(char: str = "─", width: int = 60) -> str:
    return char * width


def _prompt(description: str, default: str, current: str) -> str:
    """
    Show description, default, and current value.
    User presses Enter (or Y/y) to accept current value.
    Typing N/n re-prompts for a new value.
    Typing anything else uses it directly as the new value.
    Returns the chosen value.
    """
    print(f"\n  {description}")
    if current != default:
        print(f"  Default : {default}")
        print(f"  Current : {current}")
    else:
        print(f"  Default : {default}")
    raw = input("  Accept? [Y/N or new value]: ").strip()
    if raw == "" or raw.lower() == "y":
        return current
    if raw.lower() == "n":
        new_val = input(f"  Enter new value (current: {current}): ").strip()
        return new_val if new_val else current
    return raw


# ---------------------------------------------------------------------------
# Main questionnaire
# ---------------------------------------------------------------------------

def _run_questionnaire(
    existing: configparser.ConfigParser,
    defaults: dict[str, dict[str, str]],
) -> dict[str, dict[str, str]]:
    """
    Walk through every setting.  Returns a dict of {section: {key: value}}
    containing ALL settings (changed or not) so the user.conf is complete.
    """
    chosen: dict[str, dict[str, str]] = {}

    for section, keys in defaults.items():
        print(f"\n{'='*60}")
        print(f"  {SECTION_LABELS.get(section, section.upper())}")
        print(_divider())

        chosen[section] = {}
        for key, default_val in keys.items():
            current_val = existing.get(section, key, fallback=default_val)
            desc        = DESCRIPTIONS.get(section, {}).get(key, key)
            chosen[section][key] = _prompt(desc, default_val, current_val)

    return chosen


# ---------------------------------------------------------------------------
# Summary + confirmation
# ---------------------------------------------------------------------------

def _show_summary(
    chosen: dict[str, dict[str, str]],
    defaults: dict[str, dict[str, str]],
) -> None:
    print(f"\n{'='*60}")
    print("  CONFIGURATION SUMMARY")
    print(_divider())
    changed_any = False
    for section, keys in chosen.items():
        section_header_printed = False
        for key, val in keys.items():
            default_val = defaults.get(section, {}).get(key, "")
            marker = "  *" if val != default_val else "   "
            if not section_header_printed:
                print(f"\n  [{section}]")
                section_header_printed = True
            print(f"{marker}  {key} = {val}")
            if val != default_val:
                changed_any = True
    if not changed_any:
        print("\n  (no changes from defaults)")
    else:
        print("\n  * = changed from default")


# ---------------------------------------------------------------------------
# Write user.conf
# ---------------------------------------------------------------------------

def _write_user_conf(
    chosen: dict[str, dict[str, str]],
    dest_path: str,
) -> None:
    cfg = configparser.ConfigParser()
    for section, keys in chosen.items():
        cfg[section] = keys
    Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
    with open(dest_path, "w", encoding="utf-8") as fh:
        fh.write("# bios_preservation_user.conf\n")
        fh.write("# Generated by bios_configure.py — do not edit by hand.\n")
        fh.write("# Delete this file to restore defaults.\n\n")
        cfg.write(fh)
    print(f"\n  User configuration written → {dest_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(config: configparser.ConfigParser, base_dir: str = ".") -> bool:
    user_conf_path = str(Path(base_dir) / "configure" / "bios_preservation_user.conf")

    print(f"\n{'='*60}")
    print("  BIOS Preservation — Configure")
    print(_divider())
    print("  1. Edit user configuration")
    print("  2. Restore defaults  (deletes user.conf)")
    print(_divider())

    choice = input("  Enter number: ").strip()

    # ── Restore defaults ──────────────────────────────────────────────────
    if choice == "2":
        if os.path.exists(user_conf_path):
            os.remove(user_conf_path)
            print(f"\n  Deleted: {user_conf_path}")
            print("  Defaults will be used on next run.")
        else:
            print("\n  No user configuration found — defaults already active.")
        return True

    if choice != "1":
        print("  Invalid selection. Returning to menu.")
        return False

    # ── Edit configuration ────────────────────────────────────────────────
    print("\n  Press Enter or Y to accept the shown value.")
    print("  Type a new value and press Enter to change it.")

    # Load existing user conf if present, so current values are shown
    existing = configparser.ConfigParser()
    existing.read_dict({s: dict(k) for s, k in config.items() if s != "DEFAULT"})
    if os.path.exists(user_conf_path):
        existing.read(user_conf_path, encoding="utf-8")

    chosen = _run_questionnaire(existing, DEFAULTS)

    _show_summary(chosen, DEFAULTS)

    print(f"\n{'='*60}")
    confirm = input("  Save these settings? [Y/N]: ").strip().lower()
    if confirm in ("", "y"):
        _write_user_conf(chosen, user_conf_path)
        print("  Configuration saved.  Changes take effect on next run.")
    else:
        print("  Cancelled — no changes written.")

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
