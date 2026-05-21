"""
bios_restore.py — Script 7 (restore)

Restores the database from a backup zip produced by bios_backup.py.

Unlike bios_build.py, this script understands the backup format directly.
It uses the .blob_map.json sidecar (written by bios_backup.py) to map every
blob back to its canonical name without relying on the current manifest.
This makes restoration robust across manifest updates.

For each blob in the backup:
  - canonical still in current manifest  → stored normally
  - canonical no longer in manifest      → written to orphans_dir as
                                           {md5}.{ext}, alongside
                                           _orphans.json mapping
                                           {sqlar_name: {canonical_name, status}}

Alias relationships are restored from the .aliases.json sidecar.

After restore, run Report to check collection status.  If the manifest has
changed substantially, run Update then Build (without sources) to pick up
newly declared files; the orphans folder can then be added as a Build source
and files will be re-ingested if the new manifest declares their MD5.

e.g.  backup/20_mar_2026_backup.zip  →  restore  →  build/bios_database.sqlar
"""

from __future__ import annotations

import configparser
import hashlib
import json
import os
import sqlite3
import sys
import time
import zipfile
from pathlib import Path


# ---------------------------------------------------------------------------
# Path helper
# ---------------------------------------------------------------------------

def _resolve(path: str, base_dir: str) -> str:
    p = Path(path)
    return str(p if p.is_absolute() else Path(base_dir) / p)



# ---------------------------------------------------------------------------
# Backup selection
# ---------------------------------------------------------------------------

def _select_backup(backup_dir: str) -> str | None:
    """
    List available backup zips in backup_dir, let the user pick one by number,
    or enter a custom path.  Returns the selected path, or None to cancel.
    """
    zips: list[str] = []
    if os.path.isdir(backup_dir):
        zips = sorted(
            [os.path.join(backup_dir, f) for f in os.listdir(backup_dir)
             if f.endswith(".zip")],
            key=os.path.getmtime,
            reverse=True,
        )

    print(f"\n{'='*60}")
    print("  RESTORE — Select backup zip")
    print(f"{'─'*60}")

    if zips:
        print(f"  Available backups in {backup_dir!r}:\n")
        for i, z in enumerate(zips, 1):
            size_mb = os.path.getsize(z) / (1024 * 1024)
            name    = os.path.basename(z)
            print(f"  [{i}] {name}  ({size_mb:.0f} MB)")
        print()
        print("  [P] Enter a custom path")
        print("  [0] Cancel")
        print(f"{'─'*60}")

        raw = input("  Choice: ").strip()
        if raw == "0":
            return None
        if raw.upper() == "P":
            path = input("  Enter path to backup zip: ").strip().strip('"').strip("'")
            return path if path else None
        if raw.isdigit() and 1 <= int(raw) <= len(zips):
            return zips[int(raw) - 1]
        print("  Invalid selection.")
        return None
    else:
        print(f"  No backup zips found in {backup_dir!r}.")
        print()
        raw = input("  Enter path to backup zip (or Enter to cancel): ").strip().strip('"').strip("'")
        return raw if raw else None


# ---------------------------------------------------------------------------
# Database helpers (reuse schema from bios_build)
# ---------------------------------------------------------------------------

def _init_db(conn: sqlite3.Connection) -> None:
    import bios_build as bb
    bb.init_db(conn)


def _store_blob(
    conn: sqlite3.Connection,
    sqlar_name: str,
    canonical_name: str,
    data: bytes,
    status: str,
    hashes: dict,
    manifest_entry: dict | None,
) -> None:
    import bios_build as bb
    mtime = int(time.time())
    sz    = len(data)
    conn.execute(
        "INSERT OR REPLACE INTO sqlar (name, mode, mtime, sz, data) VALUES (?,?,?,?,?)",
        (sqlar_name, 0o644, mtime, sz, data),
    )
    conn.execute(
        "INSERT OR REPLACE INTO files "
        "(sqlar_name, canonical_name, sha1, md5, sha256, crc32, size, status) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            sqlar_name, canonical_name,
            hashes.get("sha1", ""), hashes.get("md5", ""),
            hashes.get("sha256", ""), hashes.get("crc32", ""),
            hashes.get("size", sz), status,
        ),
    )
    if manifest_entry:
        import bios_build as bb
        for p in bb.PLATFORMS:
            pinfo = (manifest_entry.get("platforms") or {}).get(p) or {}
            if pinfo.get("known_file"):
                required = 1 if pinfo.get("required") else 0
                conn.execute(
                    "INSERT OR IGNORE INTO file_platforms "
                    "(sqlar_name, platform, system, required) VALUES (?,?,?,?)",
                    (sqlar_name, p, "unknown", required),
                )
            for ht in bb.HASH_TYPES:
                for hv in (pinfo.get("expected_hashes") or {}).get(ht, []):
                    if hv:
                        conn.execute(
                            "INSERT OR IGNORE INTO accepted_hashes "
                            "(sqlar_name, hash_type, hash_value, declared_by) VALUES (?,?,?,?)",
                            (sqlar_name, ht, hv.lower(), p),
                        )


def _hash_bytes(data: bytes) -> dict:
    import zlib
    return {
        "md5":    hashlib.md5(data).hexdigest(),
        "sha1":   hashlib.sha1(data).hexdigest(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "crc32":  f"{zlib.crc32(data) & 0xFFFFFFFF:08x}",
        "size":   len(data),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(config: configparser.ConfigParser, base_dir: str = ".") -> bool:
    section = "restore"

    sqlar_output  = _resolve(
        config.get("build", "sqlar_output", fallback="build/bios_database.sqlar"),
        base_dir,
    )
    json_output   = _resolve(
        config.get("build", "json_output",  fallback="build/combined_platform_build.json"),
        base_dir,
    )
    csv_output    = _resolve(
        config.get("build", "csv_output",   fallback="build/combined_platform_build.csv"),
        base_dir,
    )
    manifest_input = _resolve(
        config.get("build", "manifest_input",
                   fallback="update/combined_platform_manifest.json"),
        base_dir,
    )
    backup_dir    = _resolve(
        config.get("backup", "backup_dir",   fallback="backup"),
        base_dir,
    )
    orphans_dir   = _resolve(
        config.get(section, "orphans_dir",  fallback="orphans"),
        base_dir,
    )

    # ── Select backup zip ────────────────────────────────────────────────────
    backup_path = _select_backup(backup_dir)
    if not backup_path:
        print("[restore] Cancelled.")
        return False
    if not os.path.exists(backup_path):
        print(f"[restore] ERROR: backup zip not found: {backup_path!r}")
        return False

    # ── Validate it's a tool-produced backup ────────────────────────────────
    try:
        with zipfile.ZipFile(backup_path, "r") as zf:
            members = set(zf.namelist())
    except Exception as exc:
        print(f"[restore] ERROR: could not open backup zip: {exc}")
        return False

    if ".blob_map.json" not in members:
        print(f"[restore] ERROR: {os.path.basename(backup_path)!r} has no .blob_map.json sidecar.")
        print("[restore] This backup was produced before the restore feature was added.")
        print("[restore] Use bios_build.py with the backup zip as a source instead,")
        print("[restore] then re-backup to produce a restore-compatible archive.")
        return False

    # ── Load current manifest (for canonical validation and manifest entries) ─
    print(f"[restore] Loading manifest from {manifest_input!r} …")
    if not os.path.exists(manifest_input):
        print(f"[restore] ERROR: manifest not found: {manifest_input!r}")
        print("[restore] Run Update first.")
        return False
    with open(manifest_input, "r", encoding="utf-8") as fh:
        source_manifest: dict = json.load(fh)
    canonical_map: dict = source_manifest.get("files") or {}
    print(f"  {len(canonical_map)} canonical(s) in current manifest.")

    # ── Confirm wipe ───────────────────────────────────────────────────────
    print(f"\n[restore] Source  : {backup_path!r}")
    print(f"[restore] Target  : {sqlar_output!r}")
    print(f"[restore] Orphans : {orphans_dir!r}")

    if os.path.exists(sqlar_output):
        print(f"\n  WARNING: existing database will be deleted and replaced.")
        raw = input("  Proceed? [Y/N]: ").strip().lower()
        if raw not in ("y", ""):
            print("[restore] Cancelled.")
            return False
        os.unlink(sqlar_output)
        print(f"[restore] Existing database removed.")

    # ── Read sidecars ──────────────────────────────────────────────────────
    with zipfile.ZipFile(backup_path, "r") as zf:
        blob_map_raw  = json.loads(zf.read(".blob_map.json").decode("utf-8"))
        alias_entries = (
            json.loads(zf.read(".aliases.json").decode("utf-8"))
            if ".aliases.json" in members else []
        )

    # blob_map: {sqlar_name -> {canonical_name, status}}
    blob_map: dict[str, dict] = blob_map_raw

    print(f"[restore] Backup has {len(blob_map)} blob(s), {len(alias_entries)} alias(es).")

    # ── Open fresh database ────────────────────────────────────────────────
    Path(sqlar_output).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(sqlar_output)
    conn.execute("PRAGMA page_size = 4096")
    _init_db(conn)

    os.makedirs(orphans_dir, exist_ok=True)

    # ── Process blobs ──────────────────────────────────────────────────────
    counts   = {"verified": 0, "unverifiable": 0, "mismatch_accepted": 0}
    orphaned = 0
    skipped  = 0
    orphans_index: dict[str, dict] = {}   # sqlar_name → {canonical_name, status}

    SUBFOLDERS = {"verified", "unverifiable", "mismatch_accepted"}

    print(f"[restore] Restoring …")

    with zipfile.ZipFile(backup_path, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            fname = info.filename
            # Skip sidecars
            if fname.startswith(".") or "/" not in fname:
                continue
            subfolder = fname.split("/")[0]
            if subfolder not in SUBFOLDERS:
                continue

            member_name = fname.split("/")[-1]

            # Read data first — needed to compute MD5 for non-verified blob_map lookup.
            try:
                data = zf.read(fname)
            except Exception as exc:
                print(f"  WARN: could not read {fname!r}: {exc}")
                skipped += 1
                continue

            hashes = _hash_bytes(data)

            # Resolve sqlar_name and look up blob_map entry.
            #
            # Verified blobs are stored in the backup as {sqlar_name} directly
            # (already hash-based), so the member filename IS the sqlar_name.
            #
            # Non-verified (unverifiable / mismatch_accepted) blobs are stored
            # under their staging-path basename (e.g. "boot.rom"), which is the
            # canonical name — not the hash-based sqlar_name.  Reconstruct it
            # from the computed MD5 and the member's extension.
            if subfolder == "verified":
                sqlar_name = member_name
            else:
                ext = os.path.splitext(member_name)[1].lower() or ".bin"
                sqlar_name = f"{hashes['md5']}{ext}"

            bmap_entry = blob_map.get(sqlar_name)
            if not bmap_entry:
                print(f"  WARN: no blob_map entry for {member_name!r} — skipping.")
                skipped += 1
                continue

            canonical_name = bmap_entry["canonical_name"]
            original_status = bmap_entry["status"]

            if canonical_name in canonical_map:
                # Still in manifest — store normally
                manifest_entry = canonical_map[canonical_name]
                _store_blob(conn, sqlar_name, canonical_name, data,
                            original_status, hashes, manifest_entry)
                counts[original_status] = counts.get(original_status, 0) + 1
            else:
                # Orphaned — canonical no longer in manifest
                orphan_filename = sqlar_name  # already hash-named in backup
                orphan_path = os.path.join(orphans_dir, orphan_filename)
                with open(orphan_path, "wb") as fh:
                    fh.write(data)
                orphans_index[sqlar_name] = {
                    "canonical_name": canonical_name,
                    "original_status": original_status,
                    "md5": hashes["md5"],
                    "sha1": hashes["sha1"],
                    "size": hashes["size"],
                }
                orphaned += 1

    conn.commit()

    # ── Restore aliases ────────────────────────────────────────────────────
    alias_restored = 0
    alias_skipped  = 0
    for entry in alias_entries:
        canonical  = entry.get("canonical_name", "")
        sqlar_name = entry.get("sqlar_name", "")
        if not canonical or not sqlar_name:
            continue
        exists = conn.execute(
            "SELECT 1 FROM sqlar WHERE name = ?", (sqlar_name,)
        ).fetchone()
        if exists:
            conn.execute(
                "INSERT OR IGNORE INTO canonical_aliases (canonical_name, sqlar_name) "
                "VALUES (?, ?)",
                (canonical, sqlar_name),
            )
            alias_restored += 1
        else:
            alias_skipped += 1
    conn.commit()

    if alias_restored:
        print(f"  Restored {alias_restored} alias mapping(s).")
    if alias_skipped:
        print(f"  Skipped  {alias_skipped} alias(es) whose target blob was not restored "
              f"(canonical may have been orphaned).")

    # ── Write orphans index ────────────────────────────────────────────────
    if orphans_index:
        idx_path = os.path.join(orphans_dir, "_orphans.json")
        with open(idx_path, "w", encoding="utf-8") as fh:
            json.dump(orphans_index, fh, indent=2)
        print(f"\n[restore] {orphaned} orphaned blob(s) written to {orphans_dir!r}")
        print(f"  These canonicals are no longer in the current manifest.")
        print(f"  Orphan index: {idx_path!r}")
        print(f"  To recover: run Update, then add {orphans_dir!r} as a Build source.")

    # ── Write build manifest ───────────────────────────────────────────────
    # Use the same meta key bios_build.py reads ('manifest_generated_at') so
    # the next Build after a Restore can detect a manifest change and run
    # audit_sqlar.  Writing 'generated_at' here would leave the build's lookup
    # returning None, suppressing the audit indefinitely.
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('manifest_generated_at', ?)",
        (source_manifest.get("generated_at", ""),)
    )
    conn.commit()

    print(f"\n[restore] Writing build manifests …")
    try:
        import bios_build as bb
        bb.write_build_manifest(source_manifest, conn, json_output, csv_output)
    except Exception as exc:
        print(f"  WARNING: could not write build manifest: {exc}")

    # ── Collection summary ─────────────────────────────────────────────────
    import bios_build as bb
    bb.populate_missing_files(conn, canonical_map, set())
    missing_count = conn.execute(
        "SELECT COUNT(DISTINCT canonical_name) FROM missing_files"
    ).fetchone()[0]
    alias_count = conn.execute(
        "SELECT COUNT(DISTINCT canonical_name) FROM canonical_aliases"
    ).fetchone()[0]

    canonical_status: dict[str, str] = {}
    for _sq, canonical, status in conn.execute(
        "SELECT sqlar_name, canonical_name, status FROM files"
    ):
        existing = canonical_status.get(canonical)
        if existing is None or bb.STATUS_RANK.get(status, 99) < bb.STATUS_RANK.get(existing, 99):
            canonical_status[canonical] = status

    db_verified     = sum(1 for s in canonical_status.values() if s == "verified")
    db_unverifiable = sum(1 for s in canonical_status.values() if s == "unverifiable")
    db_mismatch     = sum(1 for s in canonical_status.values() if s == "mismatch_accepted")
    db_present      = len(canonical_status)
    db_total        = db_present + missing_count + alias_count

    total_blobs    = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    verified_blobs = conn.execute("SELECT COUNT(*) FROM files WHERE status='verified'").fetchone()[0]

    conn.close()

    total_restored = sum(counts.values())
    print(f"\n[restore] Done.  "
          f"verified={counts.get('verified',0)}  "
          f"unverifiable={counts.get('unverifiable',0)}  "
          f"mismatch_accepted={counts.get('mismatch_accepted',0)}  "
          f"orphaned={orphaned}  skipped={skipped}  "
          f"total_restored={total_restored}")

    print(f"\n[restore] Collection summary — {db_total} canonical(s) across all platforms:")
    print(f"  Present  : {db_present:>6}  (at least one blob stored)")
    print(f"    verified          : {db_verified:>6}")
    print(f"    unverifiable      : {db_unverifiable:>6}")
    print(f"    hash mismatch     : {db_mismatch:>6}")
    if alias_count:
        print(f"  Via alias: {alias_count:>6}  (bytes stored under a different canonical name)")
    print(f"  Missing  : {missing_count:>6}  (not in current manifest or orphaned)")
    if orphaned:
        print(f"  Orphaned : {orphaned:>6}  (canonical no longer in current manifest — see {orphans_dir!r})")
    print(f"\n  Blobs stored : {total_blobs} total  ({verified_blobs} verified)")

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
