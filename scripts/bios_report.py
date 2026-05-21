"""
bios_report.py — Script 4 (report)

Generates one CSV report per enabled platform from the build manifest and
sqlar database.

Report columns per row (one row per platform-expected filename):
  filename          — the filename the platform expects (from staging path)
  present           — yes / no
  staging_path      — full relative path the platform expects (col 3)
  actual size       — size in bytes from sqlar, or blank if not present
  expected size     — not present / match / number if different
  actual sha1       — from sqlar, or blank if not present
  expected sha1     — not present / match / declared value(s) if mismatch
  actual md5        — from sqlar, or blank if not present
  expected md5      — not present / match / declared value(s) if mismatch
  actual sha256     — from sqlar, or blank if not present
  expected sha256   — not present / match / declared value(s) if mismatch
  actual crc32      — from sqlar, or blank if not present
  expected crc32    — not present / match / declared value(s) if mismatch

Output: report/<platform>_report.csv
"""

from __future__ import annotations

import configparser
import csv
import json
import os
import sqlite3
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLATFORMS = [
    "retrodeck", "retropie", "batocera", "emudeck",
    "recalbox", "retrobat", "lakka", "retroarch",
    "romm", "bizhawk",
]
HASH_TYPES = ("sha1", "md5", "sha256", "crc32")

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

# Default platform selection (all on for report)
_REPORT_DEFAULTS: dict[str, bool] = {p: True for p in PLATFORMS}

REPORT_HEADERS = [
    "filename",
    "present",
    "staging_path",
    "actual size",  "expected size",
    "actual sha1",  "expected sha1",
    "actual md5",   "expected md5",
    "actual sha256","expected sha256",
    "actual crc32", "expected crc32",
]


# ---------------------------------------------------------------------------
# Platform selection prompt
# ---------------------------------------------------------------------------

def _confirm_platforms(config: configparser.ConfigParser) -> list[str]:
    """
    Show the current platform defaults and let the user toggle them per-run.
    Selections are NOT saved to the config file — they apply to this run only.
    Returns the list of platform names to report on.
    """
    current: dict[str, bool] = {}
    for p in PLATFORMS:
        raw = config.get("report", p, fallback=None)
        if raw is not None:
            current[p] = raw.strip().lower() in ("yes", "true", "1")
        else:
            current[p] = _REPORT_DEFAULTS[p]

    while True:
        print(f"\n{'='*60}")
        print("  REPORT — Platform Selection")
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
        print(f"\n  Reporting: {', '.join(PLATFORM_DISPLAY[p] for p in enabled)}")
    else:
        print("\n  No platforms selected.")
    return enabled


# ---------------------------------------------------------------------------
# Path helper
# ---------------------------------------------------------------------------

def _load_known_upstream_issues(path: str) -> dict[str, str]:
    """
    Load the user-maintained registry of expected MD5s known to be stale,
    misprinted, or otherwise unmatched by any file in any public distribution.

    Returns ``{md5_lowercase: note_text}``.  A missing file means no known
    issues are configured and the report's Notes column will be blank —
    the feature is opt-in via the file's presence.

    File format: one entry per line, ``md5,note`` (comma-separated).  The
    note may contain commas; the split is on the FIRST comma only.  Lines
    starting with ``#`` are comments; blank lines are ignored.  Invalid
    MD5s (wrong length or non-hex) are skipped without warning so the
    file can carry organisational structure freely.
    """
    if not os.path.exists(path):
        return {}
    issues: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                parts = stripped.split(",", 1)
                if len(parts) != 2:
                    continue
                md5  = parts[0].strip().lower()
                note = parts[1].strip()
                if len(md5) == 32 and all(c in "0123456789abcdef" for c in md5):
                    issues[md5] = note
    except OSError as e:
        print(f"[report] WARNING: could not read {path!r}: {e}")
        return {}
    return issues


def _resolve(path: str, base_dir: str) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = Path(base_dir) / p
    return str(p)


def _load_known_upstream_issues(path: str) -> dict[str, str]:
    """
    Load a user-maintained list of expected MD5 values whose upstream manifest
    entry is known to be stale or incorrect.  Used to annotate the Notes
    column of shopping_hash_mismatch.csv.

    File format: CSV with at least these two columns named in the header row:
        expected_md5  — the declared hash to match against (any case; lowercased here)
        note          — free-form text written verbatim into the Notes column
    Other columns (e.g. `canonical`) are ignored by the loader; they exist to
    help whoever maintains the file find entries by eye.

    Missing or unreadable file -> empty dict (the feature is optional; a user
    who hasn't curated such a list simply gets no Notes column annotations).
    """
    if not path or not os.path.exists(path):
        return {}
    out: dict[str, str] = {}
    try:
        # utf-8-sig strips an optional BOM that spreadsheet apps sometimes add
        # when the user edits the file in Excel/LibreOffice and saves as CSV.
        with open(path, "r", newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                md5  = (row.get("expected_md5") or "").strip().lower()
                note = (row.get("note") or "").strip()
                if md5 and note:
                    out[md5] = note
    except (OSError, csv.Error) as e:
        print(f"[report] WARNING: could not read known-issues file {path!r}: {e}")
    return out


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _get_file_row(conn: sqlite3.Connection, canonical: str, fdata: dict | None = None) -> dict | None:
    """
    Return the best files table row for *canonical* (single-variant lookup).
    Used by the shopping list accumulation where we need one representative row.
    """
    rows = _get_file_rows(conn, canonical, fdata)
    if not rows:
        return None
    # Return the highest-status (best) variant
    rank = {"verified": 1, "unverifiable": 2, "mismatch_accepted": 3}
    return min(rows, key=lambda r: rank.get(r["status"], 99))


def _get_file_rows(conn: sqlite3.Connection, canonical: str, fdata: dict | None = None) -> list[dict]:
    """
    Return ALL stored variants for *canonical* as a list of row dicts.
    Falls back through alias lookups exactly as _get_file_row did.
    Returns an empty list if the file is genuinely absent.
    """
    rows = conn.execute(
        "SELECT sha1, md5, sha256, crc32, size, status FROM files WHERE canonical_name = ?",
        (canonical,),
    ).fetchall()

    if not rows and fdata:
        # Hash-based fallback for alias canonicals
        for _p, pinfo in (fdata.get("platforms") or {}).items():
            for ht in ("md5", "sha1", "sha256", "crc32"):
                for hv in (pinfo.get("expected_hashes") or {}).get(ht, []):
                    if not hv:
                        continue
                    r = conn.execute(
                        f"SELECT sha1, md5, sha256, crc32, size, status"
                        f" FROM files WHERE {ht} = ?",
                        (hv.lower(),),
                    ).fetchone()
                    if r is not None:
                        rows = [r]
                        break
                if rows:
                    break
            if rows:
                break

    if not rows and fdata:
        # database_filename fallback
        db_filename = fdata.get("database_filename", "")
        if db_filename and not str(db_filename).isdigit():
            r = conn.execute(
                "SELECT sha1, md5, sha256, crc32, size, status FROM files WHERE sqlar_name = ?",
                (db_filename,),
            ).fetchone()
            if r:
                rows = [r]

    if not rows:
        # canonical_aliases fallback — covers Cases 1–3 where the canonical has
        # no direct files entry (bytes stored under a different canonical_name).
        alias = conn.execute(
            "SELECT sqlar_name FROM canonical_aliases WHERE canonical_name = ?",
            (canonical,),
        ).fetchone()
        if alias:
            r = conn.execute(
                "SELECT sha1, md5, sha256, crc32, size, status FROM files WHERE sqlar_name = ?",
                (alias[0],),
            ).fetchone()
            if r:
                rows = [r]

    # Always append any additional canonical_aliases entries not already collected.
    # This covers Case 4 multi-variant canonicals: a canonical may have a direct
    # files entry (one platform's version) AND alias entries pointing to alternate
    # verified blobs for other platforms.  Without this, the per-platform report
    # only sees the direct files entry and misses the alternate variants.
    seen_md5s = {r[1] for r in rows if r[1]}
    alias_extras = conn.execute(
        "SELECT sqlar_name FROM canonical_aliases WHERE canonical_name = ?",
        (canonical,),
    ).fetchall()
    for (alias_sqlar,) in alias_extras:
        r = conn.execute(
            "SELECT sha1, md5, sha256, crc32, size, status FROM files WHERE sqlar_name = ?",
            (alias_sqlar,),
        ).fetchone()
        if r and r[1] not in seen_md5s:
            rows.append(r)
            if r[1]:
                seen_md5s.add(r[1])

    return [
        {
            "sha1":   r[0] or "",
            "md5":    r[1] or "",
            "sha256": r[2] or "",
            "crc32":  r[3] or "",
            "size":   r[4],
            "status": r[5] or "",
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Hash comparison helper
# ---------------------------------------------------------------------------

def _expected_hash_cell(
    actual: str,
    declared: list[str],
) -> str:
    """
    Return the value for an expected_<hash> cell:
      - 'not present'  if the platform declares no hash of this type
      - 'match'        if actual matches any declared value
      - declared value(s) joined by '|'  if declared but no match
    """
    if not declared:
        return "not present"
    actual_lower = actual.lower()
    for d in declared:
        if d.lower() == actual_lower:
            return "match"
    # Mismatch — show what was expected so the user can compare
    return ",".join(declared)


# ---------------------------------------------------------------------------
# Per-platform report
# ---------------------------------------------------------------------------

def generate_platform_report(
    conn: sqlite3.Connection,
    manifest: dict,
    platform_name: str,
    output_path: str,
) -> dict:
    """
    Write a CSV report for *platform_name* to *output_path*.
    Returns a summary dict with both canonical-file counts and staging-path counts.
    """
    # Physical-file counts — deduplicated by database_filename (matches build output)
    seen_db: dict[str, str] = {}   # database_filename -> status ("missing" if absent)
    # Manifest-entry counts — one increment per manifest entry (canonical name)
    summary = {
        "db_total":        0,
        "db_present":      0,
        "db_verified":     0,
        "db_unverifiable": 0,
        "db_mismatch":     0,
        "db_missing":      0,
        # Manifest-entry level
        "entry_total":    0,
        "entry_present":  0,
        "entry_missing":  0,
        "entry_mismatch": 0,
        # Staging-path level
        "path_total":   0,
        "path_present": 0,
        "path_missing": 0,
    }

    # Collect all rows first so we can write the summary header after counting
    rows_out: list[dict] = []

    for canonical, fdata in manifest["files"].items():
        pinfo = (fdata.get("platforms") or {}).get(platform_name) or {}
        if not pinfo.get("known_file"):
            continue

        db_filename              = fdata["database_filename"]
        staging_paths: list[str] = pinfo.get("staging_paths") or []
        expected_hashes: dict    = pinfo.get("expected_hashes") or {}
        expected_size            = pinfo.get("expected_size")

        paths = staging_paths if staging_paths else [canonical]

        all_variants = _get_file_rows(conn, canonical, fdata)
        present      = bool(all_variants)
        # For summary counts, use the best (highest-status) variant
        best_variant = None
        if all_variants:
            rank = {"verified": 1, "unverifiable": 2, "mismatch_accepted": 3}
            best_variant = min(all_variants, key=lambda r: rank.get(r["status"], 99))

        # ── Physical-file counts (deduplicated by canonical name) ────────────
        # Use THIS PLATFORM's perspective, not the global blob status.  A blob
        # can be globally `verified` (matched some other platform's hash) while
        # still being a mismatch from this platform's perspective.  Mirroring
        # the shopping-list logic via _sl_status_for_platform keeps Row 1 of the
        # CSV summary header consistent with Row 2 (entry_mismatch) and with
        # the shopping list itself.
        if canonical not in seen_db:
            if not present:
                platform_status = "missing"
            else:
                declared_any = any(
                    (pinfo.get("expected_hashes") or {}).get(ht)
                    for ht in HASH_TYPES
                )
                if not declared_any:
                    platform_status = "unverifiable"
                else:
                    platform_status = "hash_mismatch"
                    for variant in all_variants:
                        s, _ = _sl_status_for_platform(variant, pinfo)
                        if s is None:
                            platform_status = "verified"
                            break
            seen_db[canonical] = platform_status
            summary["db_total"] += 1
            if present:
                summary["db_present"] += 1
                if platform_status == "verified":
                    summary["db_verified"] += 1
                elif platform_status == "unverifiable":
                    summary["db_unverifiable"] += 1
                elif platform_status == "hash_mismatch":
                    summary["db_mismatch"] += 1
            else:
                summary["db_missing"] += 1

        # ── Manifest-entry counts ────────────────────────────────────────────
        summary["entry_total"] += 1
        if present:
            summary["entry_present"] += 1
        else:
            summary["entry_missing"] += 1

        # ── Staging-path counts ──────────────────────────────────────────────
        summary["path_total"]   += len(paths)
        if present:
            summary["path_present"] += len(paths)
        else:
            summary["path_missing"] += len(paths)

        # ── Build rows: one per staging path × one per stored variant ─────────
        # When multiple variants are stored, each gets its own row so the user
        # can see which variant matches which platform expectation.
        # Determine which variants to emit: if multiple verified exist, emit all;
        # otherwise emit just the best variant (same as before).
        variants_to_emit = all_variants if len(all_variants) > 1 else (all_variants or [None])

        for staging_path in paths:
            filename = staging_path.split("/")[-1]

            if not present:
                row: dict = {"filename": filename, "staging_path": staging_path}
                row["present"]       = "no"
                row["actual size"]   = ""
                row["expected size"] = "not present" if expected_size is None else str(expected_size)
                for ht in HASH_TYPES:
                    row[f"actual {ht}"]   = ""
                    declared = expected_hashes.get(ht) or []
                    row[f"expected {ht}"] = "not present" if not declared else ",".join(declared)
                # Expand missing rows by declared MD5 variants as before
                exp_md5_cell = row.get("expected md5", "")
                if "," in exp_md5_cell and exp_md5_cell not in ("match", "not present"):
                    for single_md5 in [v.strip() for v in exp_md5_cell.split(",")]:
                        rows_out.append({**row, "expected md5": single_md5})
                else:
                    rows_out.append(row)
            else:
                for actual in variants_to_emit:
                    row = {"filename": filename, "staging_path": staging_path}
                    row["present"]     = "yes"
                    row["actual size"] = "" if actual["size"] is None else str(actual["size"])

                    if expected_size is None:
                        row["expected size"] = "not present"
                    elif actual["size"] is not None and int(actual["size"]) == int(expected_size):
                        row["expected size"] = "match"
                    else:
                        row["expected size"] = str(expected_size)

                    for ht in HASH_TYPES:
                        actual_val = actual.get(ht) or ""
                        declared   = expected_hashes.get(ht) or []
                        row[f"actual {ht}"]   = actual_val
                        expected_cell = _expected_hash_cell(actual_val, declared)
                        row[f"expected {ht}"] = expected_cell
                        # Per-hash-type mismatch is still shown in the row for
                        # diagnostics, but the entry_mismatch counter is driven
                        # by _sl_status_for_platform below — the same logic the
                        # shopping list uses — so a file that matches on MD5
                        # but mismatches on a stale SHA1 (YAML data-quality
                        # quirk) is not double-counted as mismatched.

                    rows_out.append(row)

        # entry_mismatch: count this canonical as a per-platform mismatch only
        # when the platform declares at least one hash AND no stored variant
        # satisfies any of those declared hashes.  Matches _sl_status_for_platform
        # semantics so the count agrees with the shopping list.
        if present and any((expected_hashes.get(ht) or []) for ht in HASH_TYPES):
            if not any(
                _sl_status_for_platform(v, pinfo)[0] is None
                for v in variants_to_emit if v is not None
            ):
                summary["entry_mismatch"] += 1

    # ── Write CSV with summary header ───────────────────────────────────────
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as fh:
        # Plain-text summary as the first two rows so it's visible in any viewer
        fh.write(
            f"# PHYSICAL FILES (matches build): "
            f"total={summary['db_total']}  "
            f"present={summary['db_present']}  "
            f"verified={summary['db_verified']}  "
            f"unverifiable={summary['db_unverifiable']}  "
            f"hash_mismatch={summary['db_mismatch']}  "
            f"missing={summary['db_missing']}\n"
        )
        fh.write(
            f"# MANIFEST ENTRIES:               "
            f"total={summary['entry_total']}  "
            f"present={summary['entry_present']}  "
            f"missing={summary['entry_missing']}  "
            f"hash_mismatch={summary['entry_mismatch']}\n"
        )
        fh.write(
            f"# STAGING PATHS:                  "
            f"total={summary['path_total']}  "
            f"present={summary['path_present']}  "
            f"missing={summary['path_missing']}  "
            f"(counts reflect unique staging paths; actual CSV rows may be higher "
            f"when a file has multiple declared MD5 variants)\n"
        )
        writer = csv.DictWriter(fh, fieldnames=REPORT_HEADERS)
        writer.writeheader()
        for row in rows_out:
            writer.writerow(row)

    return summary


# ---------------------------------------------------------------------------
# Shopping-list status helper
# ---------------------------------------------------------------------------

def _sl_status_for_platform(
    actual: dict | None,
    pinfo: dict,
) -> tuple[str | None, str]:
    """
    Determine shopping-list status for one (canonical, platform) pair using
    *this platform's* declared hashes only — DB status is intentionally ignored.

    Returns (sl_status, actual_md5):
      (None,            "")            — verified for this platform; exclude
      ("missing",       "not present") — file not in DB
      ("hash_mismatch", <actual_md5>)  — platform declares hashes, none match
      ("unverifiable",  <actual_md5>)  — platform declares no hashes at all
    """
    if actual is None:
        return "missing", "not present"

    actual_md5 = actual.get("md5") or "unknown"
    expected_hashes: dict = pinfo.get("expected_hashes") or {}
    declared_any = False

    for ht in ("md5", "sha1", "sha256", "crc32"):
        vals = [v.lower() for v in (expected_hashes.get(ht) or []) if v]
        if not vals:
            continue
        declared_any = True
        actual_val = (actual.get(ht) or "").lower()
        if actual_val and actual_val in vals:
            return None, ""   # at least one hash matches — verified

    if not declared_any:
        return "unverifiable", actual_md5

    return "hash_mismatch", actual_md5


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(config: configparser.ConfigParser, base_dir: str = ".") -> bool:
    section = "report"

    manifest_input = _resolve(
        config.get(section, "manifest_input",
                   fallback="build/combined_platform_build.json"),
        base_dir,
    )
    sqlar_input = _resolve(
        config.get(section, "sqlar_input",
                   fallback="build/bios_database.sqlar"),
        base_dir,
    )
    report_dir = _resolve(
        config.get(section, "report_dir", fallback="report"),
        base_dir,
    )
    known_issues_csv = _resolve(
        config.get(section, "known_issues_csv",
                   fallback="configure/known_upstream_hash_issues.csv"),
        base_dir,
    )
    known_issues = _load_known_upstream_issues(known_issues_csv)
    if known_issues:
        print(f"[report] Loaded {len(known_issues)} known-upstream-hash-issue "
              f"entries from {os.path.basename(known_issues_csv)}")

    # Which platforms to report on — confirmed interactively per run
    enabled: list[str] = _confirm_platforms(config)

    if not enabled:
        print("[report] WARNING: no platforms enabled.")
        return True

    # ── Load inputs ────────────────────────────────────────────────────────
    if not os.path.exists(manifest_input):
        print(f"[report] ERROR: build manifest not found: {manifest_input!r}")
        return False
    if not os.path.exists(sqlar_input):
        print(f"[report] ERROR: sqlar database not found: {sqlar_input!r}")
        return False

    with open(manifest_input, "r", encoding="utf-8") as fh:
        manifest: dict = json.load(fh)

    conn = sqlite3.connect(sqlar_input)

    # Ensure files.confidence column exists (added by verify_pass update).
    # Older databases built before this change won't have it yet.
    try:
        conn.execute("SELECT confidence FROM files LIMIT 0")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE files ADD COLUMN confidence TEXT")
        conn.commit()

    os.makedirs(report_dir, exist_ok=True)

    # ── Generate one report per platform ───────────────────────────────────
    print(f"[report] Generating reports for {len(enabled)} platform(s) → {report_dir!r}")

    overall_ok = True
    shopping: dict[str, dict] = {}  # md5 -> aggregated entry

    for p in enabled:
        output_path = os.path.join(report_dir, f"{p}_report.csv")
        try:
            summary = generate_platform_report(conn, manifest, p, output_path)
            print(
                f"  {p:12s}  "
                f"present={summary['db_present']:4d}  "
                f"verified={summary['db_verified']:4d}  "
                f"unverifiable={summary['db_unverifiable']:4d}  "
                f"hash_mismatch={summary['db_mismatch']:4d}  "
                f"missing={summary['db_missing']:4d}  "
                f"(of {summary['db_total']:4d} physical files)"
            )
            print(f"  {'':12s}  → {output_path}")
        except Exception as exc:
            print(f"  {p:12s}  ERROR: {exc}")
            overall_ok = False
            continue

    # ── Global shopping list ───────────────────────────────────────────────
    # Iterate per-canonical across all enabled platforms before writing to the
    # shopping dict.
    #
    # any_verified: tracks whether ANY platform considers this file verified.
    #   → Used only to suppress the no_md5 (unverifiable) bucket.
    #     A file verified by platform A but unverifiable by platform B doesn't
    #     need to be shopped for — we already have a good copy.
    #   → Does NOT suppress per_md5 rows. If we have version A verified but
    #     platform X needs version B, version B still belongs on the list.
    #
    # per_md5: one entry per distinct declared MD5 that didn't match.
    #   Each represents a specific version we don't yet have.
    #
    # no_md5: collects platforms that declare no hashes for this file.
    #   Only emitted when any_verified is False (nobody has confirmed it's good).

    _STATUS_RANK_SL = {"missing": 1, "hash_mismatch": 2, "unverifiable": 3}

    for canonical, fdata in manifest["files"].items():
        any_verified = False
        per_md5: dict[str, dict] = {}
        no_md5:  dict | None     = None

        # Fetch all stored variants once for this canonical
        all_variants = _get_file_rows(conn, canonical, fdata)
        verified_md5s: set[str] = {
            v["md5"].lower() for v in all_variants
            if v["status"] == "verified" and v["md5"]
        }

        # Look up verify_pass confidence for this canonical.
        # Two sources depending on how verify_pass handled it:
        #   canonical_aliases.confidence — hash-matched true alias (blob was removed)
        #   files.confidence             — platform-corroborated annotation (blob kept)
        # NULL-confidence aliases (reconcile_aliases, MD5-based) are already
        # effectively verified — we only surface 'high' / 'low' from verify_pass.
        _alias_conf = conn.execute(
            "SELECT confidence FROM canonical_aliases WHERE canonical_name = ?",
            (canonical,),
        ).fetchone()
        if _alias_conf and _alias_conf[0]:
            canonical_confidence: str = _alias_conf[0]
        else:
            _file_conf = conn.execute(
                "SELECT confidence FROM files WHERE canonical_name = ? LIMIT 1",
                (canonical,),
            ).fetchone()
            canonical_confidence = (
                _file_conf[0] if _file_conf and _file_conf[0] else ""
            )

        for p in enabled:
            pinfo = (fdata.get("platforms") or {}).get(p) or {}
            if not pinfo.get("known_file"):
                continue

            # Check all stored variants — verified if ANY satisfies this platform
            sl_status: str | None = "missing" if not all_variants else None
            actual_md5: str = "not present" if not all_variants else ""
            if all_variants:
                for variant in all_variants:
                    s, m = _sl_status_for_platform(variant, pinfo)
                    if s is None:          # this variant satisfies the platform
                        sl_status = None
                        actual_md5 = ""
                        break
                    if not actual_md5:     # record first variant's actual_md5
                        actual_md5 = m
                    if sl_status is None:
                        sl_status = s
                    elif _STATUS_RANK_SL.get(s, 99) < _STATUS_RANK_SL.get(sl_status, 99):
                        sl_status = s
                        actual_md5 = m

            if sl_status is None:
                any_verified = True
                continue

            staging_paths = pinfo.get("staging_paths") or [canonical]
            filenames: set[str] = {sp.split("/")[-1] for sp in staging_paths}
            if canonical.lower() not in {f.lower() for f in filenames}:
                filenames.add(canonical)
            display = PLATFORM_DISPLAY.get(p, p)

            declared_md5s = [v.lower() for v in
                             ((pinfo.get("expected_hashes") or {}).get("md5") or []) if v]

            if declared_md5s:
                for md5 in declared_md5s:
                    # Skip this MD5 if we already have it verified — it's covered
                    if md5 in verified_md5s:
                        continue
                    # Determine actual_md5 to report: prefer the stored variant
                    # whose MD5 is closest to this target, else use best variant
                    variant_for_md5 = next(
                        (v for v in all_variants if v["md5"].lower() == md5), None
                    )
                    act = variant_for_md5["md5"] if variant_for_md5 else actual_md5

                    if md5 not in per_md5:
                        per_md5[md5] = {"status": sl_status, "actual_md5": act,
                                        "confidence": "",
                                        "platforms": set(), "filenames": set()}
                    else:
                        if (_STATUS_RANK_SL.get(sl_status, 99)
                                < _STATUS_RANK_SL.get(per_md5[md5]["status"], 99)):
                            per_md5[md5]["status"]     = sl_status
                            per_md5[md5]["actual_md5"] = act
                    per_md5[md5]["platforms"].add(display)
                    per_md5[md5]["filenames"].update(filenames)
            else:
                effective_status = "unverifiable" if sl_status == "hash_mismatch" else sl_status
                if no_md5 is None:
                    no_md5 = {"status": effective_status, "actual_md5": actual_md5,
                              "confidence": canonical_confidence or "unresolved",
                              "platforms": set(), "filenames": set()}
                else:
                    if (_STATUS_RANK_SL.get(effective_status, 99)
                            < _STATUS_RANK_SL.get(no_md5["status"], 99)):
                        no_md5["status"]     = effective_status
                        no_md5["actual_md5"] = actual_md5
                no_md5["platforms"].add(display)
                no_md5["filenames"].update(filenames)

        # Emit per-MD5 rows regardless of any_verified — each row is a specific
        # version we don't have yet, even if we have a different verified version.
        for md5, data in per_md5.items():
            if md5 not in shopping:
                shopping[md5] = {
                    "expected_md5": md5,
                    "actual_md5":   data["actual_md5"],
                    "status":       data["status"],
                    "confidence":   "",
                    "platforms":    set(),
                    "filenames":    set(),
                }
            else:
                if (_STATUS_RANK_SL.get(data["status"], 99)
                        < _STATUS_RANK_SL.get(shopping[md5]["status"], 99)):
                    shopping[md5]["status"]       = data["status"]
                    shopping[md5]["actual_md5"]   = data["actual_md5"]
                    shopping[md5]["expected_md5"] = md5
            shopping[md5]["platforms"].update(data["platforms"])
            shopping[md5]["filenames"].update(data["filenames"])

        # Only emit the no_md5 (unverifiable/missing) row when:
        #   - no platform has verified this file (any_verified = False), AND
        #   - no platform has declared an expected MD5 (per_md5 is empty).
        # If per_md5 is non-empty, those rows already capture the actionable
        # information (what version to hunt for); a parallel unverifiable row
        # is redundant and can produce confusing status contradictions.
        if no_md5 is not None and not any_verified and not per_md5:
            if canonical not in shopping:
                shopping[canonical] = {
                    "expected_md5": "unknown",
                    "actual_md5":   no_md5["actual_md5"],
                    "status":       no_md5["status"],
                    "confidence":   no_md5["confidence"],
                    "platforms":    set(),
                    "filenames":    set(),
                }
            else:
                if (_STATUS_RANK_SL.get(no_md5["status"], 99)
                        < _STATUS_RANK_SL.get(shopping[canonical]["status"], 99)):
                    shopping[canonical]["status"]     = no_md5["status"]
                    shopping[canonical]["actual_md5"] = no_md5["actual_md5"]
                # Carry confidence through if not already set on this entry
                if not shopping[canonical].get("confidence"):
                    shopping[canonical]["confidence"] = no_md5["confidence"]
            shopping[canonical]["platforms"].update(no_md5["platforms"])
            shopping[canonical]["filenames"].update(no_md5["filenames"])

    # Write global shopping list
    if shopping:
        # Sanity pass: any entry with a known expected MD5 must be hash_mismatch,
        # not unverifiable.  The two states are mutually exclusive by definition.
        for entry in shopping.values():
            if entry["expected_md5"] != "unknown" and entry["status"] == "unverifiable":
                entry["status"] = "hash_mismatch"

        # Confidence annotation for hash_mismatch and unverifiable entries.
        # Both follow the same platform-corroboration model, derived from the
        # platforms accumulated in the shopping entry itself:
        #   high       — 2+ platforms agree on the target  (stronger signal)
        #   low        — 1 platform declares it             (weaker signal)
        #   unresolved — 0 platforms                        (rare edge case)
        # This unifies the model across statuses and removes a brittle
        # dependency on verify_pass having pre-annotated files.confidence in
        # the DB — that lookup was case-sensitive on canonical_name and could
        # silently miss when DB rows and manifest keys differ in casing,
        # causing every unverifiable shopping-list row to render as
        # "unresolved" even when many platforms corroborate the file.
        for entry in shopping.values():
            if entry["status"] in ("hash_mismatch", "unverifiable"):
                n = len(entry.get("platforms") or [])
                if n >= 2:
                    entry["confidence"] = "high"
                elif n == 1:
                    entry["confidence"] = "low"
                else:
                    entry["confidence"] = "unresolved"
        sl_path = os.path.join(report_dir, "global_shopping_list.csv")
        with open(sl_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow([
                "Known Aliases", "Expected MD5", "Status",
                "Confidence", "Platforms", "Actual MD5",
            ])
            for canonical, data in sorted(shopping.items()):
                writer.writerow([
                    ", ".join(sorted(data["filenames"])),
                    data["expected_md5"],
                    data["status"],
                    data.get("confidence", ""),
                    ", ".join(sorted(data["platforms"])),
                    data["actual_md5"],
                ])
        missing_count  = sum(1 for d in shopping.values() if d["status"] == "missing")
        mismatch_count = sum(1 for d in shopping.values() if d["status"] == "hash_mismatch")
        unverif_count  = sum(1 for d in shopping.values() if d["status"] == "unverifiable")

        mismatch_high  = sum(1 for d in shopping.values()
                             if d["status"] == "hash_mismatch" and d.get("confidence") == "high")
        mismatch_low   = sum(1 for d in shopping.values()
                             if d["status"] == "hash_mismatch" and d.get("confidence") == "low")
        mismatch_none  = mismatch_count - mismatch_high - mismatch_low

        unverif_high   = sum(1 for d in shopping.values()
                             if d["status"] == "unverifiable" and d.get("confidence") == "high")
        unverif_low    = sum(1 for d in shopping.values()
                             if d["status"] == "unverifiable" and d.get("confidence") == "low")
        unverif_none   = unverif_count - unverif_high - unverif_low

        mismatch_detail = (
            f"{mismatch_count} hash_mismatch"
            f"  [high: {mismatch_high}  low: {mismatch_low}  unresolved: {mismatch_none}]"
        )
        unverif_detail = (
            f"{unverif_count} unverifiable"
            f"  [high: {unverif_high}  low: {unverif_low}  unresolved: {unverif_none}]"
        )
        print(
            f"\n  Global shopping list → {sl_path}  "
            f"({missing_count} missing, {mismatch_detail}, {unverif_detail})"
        )

        # Write per-status subset CSVs.
        # hash_mismatch gets an extra Notes column populated from the
        # user-maintained known-upstream-hash-issues file (when present).
        # The other two CSVs are unchanged — the Notes mechanism is specific
        # to hash_mismatch, where upstream manifest errors most often surface.
        HEADERS               = [
            "Known Aliases", "Expected MD5", "Status",
            "Confidence", "Platforms", "Actual MD5",
        ]
        HEADERS_HASH_MISMATCH = HEADERS + ["Notes"]
        subsets = [
            ("missing",       "shopping_missing.csv",       missing_count,  HEADERS),
            ("hash_mismatch", "shopping_hash_mismatch.csv", mismatch_count, HEADERS_HASH_MISMATCH),
            ("unverifiable",  "shopping_unverifiable.csv",  unverif_count,  HEADERS),
        ]
        annotated = 0
        for status_key, filename, count, headers in subsets:
            subset_path = os.path.join(report_dir, filename)
            with open(subset_path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(headers)
                for canonical, data in sorted(shopping.items()):
                    if data["status"] != status_key:
                        continue
                    row = [
                        ", ".join(sorted(data["filenames"])),
                        data["expected_md5"],
                        data["status"],
                        data.get("confidence", ""),
                        ", ".join(sorted(data["platforms"])),
                        data["actual_md5"],
                    ]
                    if status_key == "hash_mismatch":
                        note = known_issues.get(
                            (data["expected_md5"] or "").lower(), ""
                        )
                        if note:
                            annotated += 1
                        row.append(note)
                    writer.writerow(row)
            print(f"  {status_key:<16} → {subset_path}  ({count} rows)")
        if known_issues and annotated:
            print(f"  {annotated} hash_mismatch row(s) annotated from known-issues file")

    conn.close()
    print("[report] Done.")
    print(
        f"\n  Reading the numbers:"
        f"\n  Each platform's 'PHYSICAL FILES' line reflects that platform's perspective."
        f"\n  Per-platform counts cover only the files that platform declares, so"
        f"\n  missing/unverifiable counts will always be lower than the build totals."
        f"\n  hash_mismatch counts may be HIGHER than the build's global total, because"
        f"\n  files the build called 'verified' can still be the wrong version for a"
        f"\n  specific platform (e.g. you have RetroBat's revision of awbios.zip but"
        f"\n  Recalbox expects a different one — globally verified, per-Recalbox mismatch)."
        f"\n  Shopping list rows also differ from the per-platform counts: the counts"
        f"\n  measure distinct files, while rows measure distinct expected MD5 versions."
        f"\n  One mismatched file with 5 declared variants produces 5 rows; the same"
        f"\n  file mismatched across 4 platforms produces 1 shared row, not 4."
    )
    return overall_ok


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
