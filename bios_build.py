"""
bios_build.py — Script 2 (build)

Scans source locations for BIOS files, verifies them against
combined_platform_manifest.json (produced by research), and stores matches
in bios_database.sqlar.

Writes its own output files to the build/ directory:
  - combined_platform_build.json  (manifest copy updated with real db filenames)
  - combined_platform_build.csv
  - bios_database.sqlar

The research/ manifest is NEVER modified by this script.
"""

from __future__ import annotations

import csv
import configparser
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
import urllib.request
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Optional archive libraries
try:
    import py7zr
    HAS_7Z = True
except ImportError:
    HAS_7Z = False

try:
    import rarfile
    HAS_RAR = True
except ImportError:
    HAS_RAR = False

import zipfile
import tarfile

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLATFORMS = [
    "retrodeck", "retropie", "batocera", "emudeck",
    "recalbox", "retrobat", "lakka", "retroarch",
    "romm", "bizhawk",
]
HASH_TYPES  = ("md5", "sha1", "sha256", "crc32")
STATUS_RANK = {"verified": 1, "unverifiable": 2, "mismatch_accepted": 3, "missing": 4}
MAX_DEPTH   = 6
SQLAR_MODE  = 0o100644


# ---------------------------------------------------------------------------
# Path helper
# ---------------------------------------------------------------------------

def _resolve(path: str, base_dir: str) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = Path(base_dir) / p
    return str(p)


# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------

def _hash_bytes(data: bytes) -> dict[str, Any]:
    crc32_val = zlib.crc32(data) & 0xFFFFFFFF
    return {
        "md5":    hashlib.md5(data).hexdigest(),
        "sha1":   hashlib.sha1(data).hexdigest(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "crc32":  format(crc32_val, "08x"),
        "size":   len(data),
    }


def _hash_file(path: str) -> dict[str, Any]:
    md5    = hashlib.md5()
    sha1   = hashlib.sha1()
    sha256 = hashlib.sha256()
    crc32_val = 0
    size = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            md5.update(chunk)
            sha1.update(chunk)
            sha256.update(chunk)
            crc32_val = zlib.crc32(chunk, crc32_val)
            size += len(chunk)
    return {
        "md5":    md5.hexdigest(),
        "sha1":   sha1.hexdigest(),
        "sha256": sha256.hexdigest(),
        "crc32":  format(crc32_val & 0xFFFFFFFF, "08x"),
        "size":   size,
    }


def _ext(filename: str) -> str:
    name = filename.lower()
    for compound in (".tar.gz", ".tar.bz2", ".tar.xz"):
        if name.endswith(compound):
            return compound
    return os.path.splitext(name)[1]


def _is_archive(name: str) -> bool:
    name = name.lower()
    return any(name.endswith(e) for e in (
        ".zip", ".7z", ".rar",
        ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz",
    ))


# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------

DB_INIT_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS sqlar (
    name   TEXT PRIMARY KEY,
    mode   INT,
    mtime  INT,
    sz     INT,
    data   BLOB
);

CREATE TABLE IF NOT EXISTS files (
    sqlar_name      TEXT PRIMARY KEY,
    canonical_name  TEXT NOT NULL,
    sha1            TEXT,
    md5             TEXT,
    sha256          TEXT,
    crc32           TEXT,
    size            INTEGER,
    status          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS file_platforms (
    sqlar_name  TEXT,
    platform    TEXT,
    system      TEXT,
    required    INTEGER,
    PRIMARY KEY (sqlar_name, platform, system)
);

CREATE TABLE IF NOT EXISTS accepted_hashes (
    sqlar_name   TEXT,
    hash_type    TEXT,
    hash_value   TEXT,
    declared_by  TEXT,
    PRIMARY KEY (sqlar_name, hash_type, hash_value)
);

CREATE TABLE IF NOT EXISTS missing_files (
    canonical_name  TEXT,
    system          TEXT,
    platform        TEXT,
    required        INTEGER,
    expected_hashes TEXT,
    PRIMARY KEY (canonical_name, system, platform)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Alias canonicals: files whose bytes are already stored under a different
-- canonical_name (same MD5, different manifest entry).  Recorded here so
-- that report lookups can resolve them without a full-table hash scan.
CREATE TABLE IF NOT EXISTS canonical_aliases (
    canonical_name  TEXT PRIMARY KEY,
    sqlar_name      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_files_canonical  ON files (canonical_name);
CREATE INDEX IF NOT EXISTS idx_aliases_sqlar    ON canonical_aliases (sqlar_name);
"""


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(DB_INIT_SQL)
    conn.commit()


# ---------------------------------------------------------------------------
# Manifest lookup helpers
# ---------------------------------------------------------------------------

def build_lookups(manifest: dict) -> tuple[dict, dict, dict]:
    canonical_map: dict[str, dict] = manifest.get("files") or {}
    hash_to_canonical: dict[str, str] = {}
    md5_to_canonical:  dict[str, str] = {}
    for canonical, fdata in canonical_map.items():
        for p in PLATFORMS:
            pinfo = (fdata.get("platforms") or {}).get(p) or {}
            for ht in HASH_TYPES:
                for hv in (pinfo.get("expected_hashes") or {}).get(ht, []):
                    if hv:
                        hash_to_canonical[hv.lower()] = canonical
                        if ht == "md5":
                            md5_to_canonical[hv.lower()] = canonical
    return canonical_map, hash_to_canonical, md5_to_canonical


def find_in_manifest(
    filename_lower: str,
    hashes: dict[str, str],
    canonical_map: dict,
    hash_to_canonical: dict,
) -> tuple[str | None, dict | None]:
    if filename_lower in canonical_map:
        return filename_lower, canonical_map[filename_lower]
    for ht in HASH_TYPES:
        hv = hashes.get(ht, "").lower()
        if hv and hv in hash_to_canonical:
            matched = hash_to_canonical[hv]
            return matched, canonical_map[matched]
    return None, None


def _all_declared_hashes(canonical: str, canonical_map: dict) -> dict[str, set[str]]:
    declared: dict[str, set[str]] = {ht: set() for ht in HASH_TYPES}
    fdata = canonical_map.get(canonical) or {}
    for p in PLATFORMS:
        pinfo = (fdata.get("platforms") or {}).get(p) or {}
        for ht in HASH_TYPES:
            for hv in (pinfo.get("expected_hashes") or {}).get(ht, []):
                if hv:
                    declared[ht].add(hv.lower())
    return declared


def determine_status(canonical: str, hashes: dict[str, str], canonical_map: dict) -> str:
    declared   = _all_declared_hashes(canonical, canonical_map)
    any_declared = any(bool(v) for v in declared.values())
    if not any_declared:
        return "unverifiable"
    for ht in HASH_TYPES:
        hv = hashes.get(ht, "").lower()
        if hv and hv in declared[ht]:
            return "verified"
    return "mismatch_accepted"


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def get_existing_status(conn: sqlite3.Connection, canonical: str) -> str | None:
    row = conn.execute(
        "SELECT status FROM files WHERE canonical_name = ? "
        "ORDER BY CASE status "
        "  WHEN 'verified'          THEN 1 "
        "  WHEN 'unverifiable'      THEN 2 "
        "  WHEN 'mismatch_accepted' THEN 3 "
        "  ELSE 4 END LIMIT 1",
        (canonical,),
    ).fetchone()
    if row:
        return row[0]

    # Alias canonicals have no row in files — they point at another canonical's
    # blob via canonical_aliases.  Fall through to the alias table so that
    # _should_store's downgrade protection fires correctly for them too.
    alias = conn.execute(
        "SELECT f.status FROM canonical_aliases ca "
        "JOIN files f ON f.sqlar_name = ca.sqlar_name "
        "WHERE ca.canonical_name = ? "
        "ORDER BY CASE f.status "
        "  WHEN 'verified'          THEN 1 "
        "  WHEN 'unverifiable'      THEN 2 "
        "  WHEN 'mismatch_accepted' THEN 3 "
        "  ELSE 4 END LIMIT 1",
        (canonical,),
    ).fetchone()
    return alias[0] if alias else None


def store_file(
    conn: sqlite3.Connection,
    sqlar_name: str,
    canonical: str,
    data: bytes,
    hashes: dict[str, Any],
    status: str,
    manifest_entry: dict,
) -> None:
    mtime = int(time.time())
    sz    = len(data)

    conn.execute(
        "INSERT OR REPLACE INTO sqlar (name, mode, mtime, sz, data) VALUES (?,?,?,?,?)",
        (sqlar_name, SQLAR_MODE, mtime, sz, data),
    )
    conn.execute(
        "INSERT OR REPLACE INTO files "
        "(sqlar_name, canonical_name, sha1, md5, sha256, crc32, size, status) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            sqlar_name, canonical,
            hashes["sha1"], hashes["md5"], hashes["sha256"], hashes["crc32"],
            hashes["size"], status,
        ),
    )

    for p in PLATFORMS:
        pinfo    = (manifest_entry.get("platforms") or {}).get(p) or {}
        if pinfo.get("known_file"):
            required = 1 if pinfo.get("required") else 0
            conn.execute(
                "INSERT OR IGNORE INTO file_platforms (sqlar_name, platform, system, required) "
                "VALUES (?,?,?,?)",
                (sqlar_name, p, "unknown", required),
            )
        for ht in HASH_TYPES:
            for hv in (pinfo.get("expected_hashes") or {}).get(ht, []):
                if hv:
                    conn.execute(
                        "INSERT OR IGNORE INTO accepted_hashes "
                        "(sqlar_name, hash_type, hash_value, declared_by) VALUES (?,?,?,?)",
                        (sqlar_name, ht, hv.lower(), p),
                    )


def remove_sqlar_entry(conn: sqlite3.Connection, sqlar_name: str) -> None:
    conn.execute("DELETE FROM sqlar          WHERE name       = ?", (sqlar_name,))
    conn.execute("DELETE FROM files          WHERE sqlar_name = ?", (sqlar_name,))
    conn.execute("DELETE FROM file_platforms WHERE sqlar_name = ?", (sqlar_name,))
    conn.execute("DELETE FROM accepted_hashes WHERE sqlar_name = ?", (sqlar_name,))


# ---------------------------------------------------------------------------
# Audit & cleanup
# ---------------------------------------------------------------------------

def _purge_orphans(conn: sqlite3.Connection, canonical_map: dict) -> int:
    """
    Remove any sqlar/files entries whose canonical_name is not present in the
    current manifest.  Also removes stale canonical_aliases entries.
    This runs on every build so orphaned blobs can never accumulate silently
    (e.g. after a manifest regeneration that removed files).
    Returns the number of entries removed.
    """
    rows = conn.execute("SELECT sqlar_name, canonical_name FROM files").fetchall()
    removed = 0
    for sqlar_name, canonical in rows:
        if canonical not in canonical_map:
            print(f"  [purge] Removing orphan {canonical!r} (not in manifest)")
            remove_sqlar_entry(conn, sqlar_name)
            removed += 1

    # Purge stale alias entries whose canonical is no longer in the manifest
    alias_rows = conn.execute(
        "SELECT canonical_name FROM canonical_aliases"
    ).fetchall()
    for (canonical,) in alias_rows:
        if canonical not in canonical_map:
            conn.execute(
                "DELETE FROM canonical_aliases WHERE canonical_name = ?", (canonical,)
            )
            removed += 1

    if removed:
        conn.commit()
    return removed


def audit_sqlar(conn: sqlite3.Connection, canonical_map: dict) -> None:
    print("  [audit] Auditing existing sqlar against updated manifest …")
    rows = conn.execute(
        "SELECT sqlar_name, canonical_name, md5, sha1, sha256, crc32, size FROM files"
    ).fetchall()

    for sqlar_name, canonical, md5, sha1, sha256, crc32, size in rows:
        if canonical not in canonical_map:
            print(f"    Removing {canonical!r}: no longer in manifest")
            remove_sqlar_entry(conn, sqlar_name)
            continue

        hashes = {
            "md5": md5 or "", "sha1": sha1 or "",
            "sha256": sha256 or "", "crc32": crc32 or "",
        }
        new_status = determine_status(canonical, hashes, canonical_map)
        old_status = conn.execute(
            "SELECT status FROM files WHERE sqlar_name = ?", (sqlar_name,)
        ).fetchone()[0]

        if STATUS_RANK.get(new_status, 99) < STATUS_RANK.get(old_status, 99):
            conn.execute(
                "UPDATE files SET status = ? WHERE sqlar_name = ?",
                (new_status, sqlar_name),
            )
            print(f"    Upgraded {canonical!r}: {old_status} → {new_status}")

    _cleanup_superseded(conn)
    conn.commit()
    print("  [audit] Done.")


def _cleanup_superseded(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT sqlar_name, canonical_name FROM files WHERE status = 'mismatch_accepted'"
    ).fetchall()
    for sqlar_name, canonical in rows:
        verified = conn.execute(
            "SELECT 1 FROM files WHERE canonical_name = ? AND status = 'verified'",
            (canonical,),
        ).fetchone()
        if verified:
            print(f"    Removing superseded mismatch_accepted blob for {canonical!r}")
            remove_sqlar_entry(conn, sqlar_name)


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

class Scanner:
    def __init__(
        self,
        conn: sqlite3.Connection,
        canonical_map: dict,
        hash_to_canonical: dict,
        md5_to_canonical: dict,
        found: set[str],
        temp_dir: str,
    ) -> None:
        self.conn              = conn
        self.canonical_map     = canonical_map
        self.hash_to_canonical = hash_to_canonical
        self._md5_to_canonical = md5_to_canonical
        self.found             = found
        self.pre_existing      = frozenset(found)   # immutable snapshot of DB state before this scan
        self.temp_dir          = temp_dir
        self.total_added       = 0
        self.total_upgraded    = 0
        self._hashscan_examined = 0
        self._hashscan_matched  = 0

    def scan_source(self, source: str) -> None:
        source = source.strip()
        # Legacy hashscan: prefix — treat as regular directory (both passes run now)
        if source.startswith("hashscan:"):
            source = source[9:]
        if source.lower().startswith(("http://", "https://")):
            self._scan_url(source)
        elif os.path.isdir(source):
            print(f"  [pass 1/2] filename + hash matching …")
            self._scan_directory(source, depth=0)
            print(f"  [pass 2/2] hash-only matching (catches renamed files) …")
            self._hashscan_examined = 0
            self._hashscan_matched  = 0
            self._scan_directory_by_hash(source, depth=0)
        elif os.path.isfile(source):
            if _is_archive(source):
                self._scan_archive_file(source, depth=0)
            else:
                self._process_file_on_disk(source, label=source)
        else:
            print(f"  WARNING: source not found or unsupported: {source!r}")

    # ── Hash-only directory scan ───────────────────────────────────────────

    def _scan_directory_by_hash(self, path: str, depth: int) -> None:
        """
        Recursively scan *path* up to MAX_DEPTH levels.  Each file is hashed
        and matched purely by MD5 against the manifest — filename is ignored.
        Archives found in the directory are opened and their contents hashed too.
        """
        if depth >= MAX_DEPTH:
            return
        try:
            entries = list(os.scandir(path))
        except (PermissionError, OSError) as exc:
            print(f"  WARNING: cannot scan {path!r}: {exc}")
            return

        if not entries:
            print(f"  WARNING: directory is empty: {path!r}")
            return

        # Show a summary per top-level call only
        if depth == 0:
            self._hashscan_examined = 0
            self._hashscan_matched  = 0

        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                self._scan_directory_by_hash(entry.path, depth + 1)
            elif entry.is_file(follow_symlinks=False):
                self._hashscan_examined += 1
                if self._hashscan_examined % 100 == 0:
                    print(f"  [hash-scan] Examined {self._hashscan_examined} files, "
                          f"matched {self._hashscan_matched} so far …")
                # Always try matching the file itself first (covers .zip BIOS files)
                self._process_file_by_hash_only(entry.path)
                # If it's also an archive, scan its contents too
                if _is_archive(entry.name):
                    print(f"  [hash-scan] Opening archive: {entry.name}")
                    self._hashscan_archive(entry.path, depth + 1, label=entry.path)

        if depth == 0:
            print(f"  [hash-scan] Done.  Files examined: {self._hashscan_examined}  "
                  f"Matched: {self._hashscan_matched}")

    def _hashscan_archive(self, path: str, depth: int, label: str) -> None:
        """Open an archive and hash-scan its contents (no filename matching).
        Each member is tried as a file first, then opened if it is itself an archive."""
        if depth >= MAX_DEPTH:
            return
        nl = path.lower()
        try:
            if nl.endswith(".zip"):
                import zipfile as _zf
                with _zf.ZipFile(path, "r") as zf:
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        member_name = info.filename.split("/")[-1]
                        data = zf.read(info.filename)
                        self._hashscan_examined += 1
                        self._hashscan_bytes(data, source_label=f"{label}!{info.filename}")
                        if _is_archive(member_name):
                            self._scan_archive_bytes(member_name, data, depth + 1, label)
            elif nl.endswith(".7z"):
                if not HAS_7Z:
                    print(f"  WARNING: py7zr not installed — skipping {path!r}")
                    return
                import py7zr as _7z, shutil as _shutil
                tmpdir = os.path.join(self.temp_dir, f"7z_{os.getpid()}_{id(path)}")
                os.makedirs(tmpdir, exist_ok=True)
                try:
                    with _7z.SevenZipFile(path, mode="r") as zf:
                        zf.extractall(path=tmpdir)
                    for root, _dirs, files in os.walk(tmpdir):
                        for fname in files:
                            fpath = os.path.join(root, fname)
                            rel = os.path.relpath(fpath, tmpdir).replace("\\", "/")
                            member_name = fname
                            with open(fpath, "rb") as fh:
                                data = fh.read()
                            self._hashscan_examined += 1
                            self._hashscan_bytes(data, source_label=f"{label}!{rel}")
                            if _is_archive(member_name):
                                self._scan_archive_bytes(member_name, data, depth + 1, label)
                except Exception as exc:
                    print(f"  WARNING: error reading .7z {label!r}: {exc}")
                finally:
                    _shutil.rmtree(tmpdir, ignore_errors=True)
            elif nl.endswith(".rar"):
                if not HAS_RAR:
                    print(f"  WARNING: rarfile not installed — skipping {path!r}")
                    return
                import rarfile as _rf
                with _rf.RarFile(path) as rf:
                    for info in rf.infolist():
                        if info.is_dir():
                            continue
                        member_name = info.filename.split("/")[-1]
                        data = rf.read(info.filename)
                        self._hashscan_examined += 1
                        self._hashscan_bytes(data, source_label=f"{label}!{info.filename}")
                        if _is_archive(member_name):
                            self._scan_archive_bytes(member_name, data, depth + 1, label)
            elif any(nl.endswith(e) for e in (".tar", ".tar.gz", ".tgz",
                                               ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
                import tarfile as _tf
                with _tf.open(path, "r:*") as tf:
                    for member in tf.getmembers():
                        if not member.isfile():
                            continue
                        member_name = member.name.split("/")[-1]
                        fobj = tf.extractfile(member)
                        if fobj is None:
                            continue
                        data = fobj.read()
                        self._hashscan_examined += 1
                        self._hashscan_bytes(data, source_label=f"{label}!{member.name}")
                        if _is_archive(member_name):
                            self._scan_archive_bytes(member_name, data, depth + 1, label)
        except Exception as exc:
            print(f"  WARNING: error scanning archive {label!r}: {exc}")

    def _hashscan_bytes(self, data: bytes, source_label: str) -> None:
        """Hash raw bytes by MD5 only and store if they match a manifest entry."""
        import hashlib, zlib
        md5 = hashlib.md5(data).hexdigest().lower()
        if md5 not in self._md5_to_canonical:
            return
        canonical = self._md5_to_canonical[md5]
        mentry = self.canonical_map.get(canonical)
        if mentry is None:
            return
        hashes = _hash_bytes(data)
        if self._should_store(canonical, hashes):
            self._store(canonical, data, hashes, mentry)
            self._hashscan_matched += 1

    # ── URL ────────────────────────────────────────────────────────────────

    def _scan_url(self, url: str) -> None:
        print(f"  Downloading {url} …")
        try:
            with urllib.request.urlopen(url, timeout=120) as resp:
                data = resp.read()
        except Exception as exc:
            print(f"  WARNING: download failed: {exc}")
            return
        name = url.rstrip("/").split("/")[-1]
        if _is_archive(name):
            os.makedirs(self.temp_dir, exist_ok=True)
            tmp_path = os.path.join(self.temp_dir, f"url_{os.getpid()}_{id(data)}{_ext(name)}")
            with open(tmp_path, "wb") as tmp:
                tmp.write(data)
            try:
                self._scan_archive_file(tmp_path, depth=0, label=url)
            finally:
                os.unlink(tmp_path)
        else:
            self._process_bytes(name, data, source_label=url, depth=0)

    # ── Directory ──────────────────────────────────────────────────────────

    def _scan_directory(self, path: str, depth: int) -> None:
        if depth >= MAX_DEPTH:
            return
        try:
            entries = list(os.scandir(path))
        except PermissionError:
            return
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                self._scan_directory(entry.path, depth + 1)
            elif entry.is_file(follow_symlinks=False):
                # Always try to match the file itself (covers MAME-style .zip BIOS files)
                self._process_file_on_disk(entry.path, label=entry.path)
                # If it's also an archive, scan its contents too
                if _is_archive(entry.name):
                    self._scan_archive_file(entry.path, depth + 1)

    # ── Archives ───────────────────────────────────────────────────────────

    def _scan_archive_file(self, path: str, depth: int, label: str | None = None) -> None:
        label = label or path
        if depth >= MAX_DEPTH:
            return
        nl = path.lower()
        try:
            if nl.endswith(".zip"):
                self._scan_zip(path, depth, label)
            elif nl.endswith(".7z"):
                self._scan_7z(path, depth, label)
            elif nl.endswith(".rar"):
                self._scan_rar(path, depth, label)
            elif any(nl.endswith(e) for e in (".tar", ".tar.gz", ".tgz",
                                               ".tar.bz2", ".tbz2",
                                               ".tar.xz", ".txz")):
                self._scan_tar(path, depth, label)
        except Exception as exc:
            print(f"  WARNING: error scanning {label!r}: {exc}")

    def _scan_zip(self, path: str, depth: int, label: str) -> None:
        with zipfile.ZipFile(path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                member_name = info.filename.split("/")[-1]
                data = zf.read(info.filename)

                # Restore canonical alias mappings from a dump sidecar.  This
                # file is written by bios_dump.py to preserve alias relationships
                # that have no declared MD5 and cannot be re-established by
                # filename or hash matching alone.
                if info.filename == ".aliases.json":
                    self._restore_aliases(data)
                    continue

                # Always try to match the member as a file (handles .zip BIOS like neogeo.zip)
                self._process_bytes(member_name, data,
                                    source_label=f"{label}!{info.filename}", depth=depth)
                # If it's also an archive, scan its contents too
                if _is_archive(member_name):
                    self._scan_archive_bytes(member_name, data, depth + 1, label)

    def _restore_aliases(self, data: bytes) -> None:
        """Restore canonical_aliases entries from a dump sidecar (.aliases.json)."""
        try:
            entries = json.loads(data.decode("utf-8"))
        except Exception as exc:
            print(f"  WARNING: could not parse .aliases.json sidecar: {exc}")
            return
        restored = 0
        for entry in entries:
            canonical = entry.get("canonical_name", "")
            sqlar_name = entry.get("sqlar_name", "")
            if not canonical or not sqlar_name:
                continue
            # Only restore the alias if the target sqlar blob actually exists
            exists = self.conn.execute(
                "SELECT 1 FROM sqlar WHERE name = ?", (sqlar_name,)
            ).fetchone()
            if exists:
                self.conn.execute(
                    "INSERT OR IGNORE INTO canonical_aliases (canonical_name, sqlar_name) "
                    "VALUES (?, ?)",
                    (canonical, sqlar_name),
                )
                self.found.add(canonical)
                restored += 1
        if restored:
            self.conn.commit()
            print(f"  [aliases] Restored {restored} alias mapping(s) from dump sidecar.")

    def _scan_7z(self, path: str, depth: int, label: str) -> None:
        if not HAS_7Z:
            print("  WARNING: py7zr not installed — skipping .7z archive")
            return
        import shutil
        tmpdir = os.path.join(self.temp_dir, f"7z_{os.getpid()}_{id(path)}")
        os.makedirs(tmpdir, exist_ok=True)
        try:
            with py7zr.SevenZipFile(path, mode="r") as zf:
                zf.extractall(path=tmpdir)
            for root, _dirs, files in os.walk(tmpdir):
                for fname in files:
                    fpath = os.path.join(root, fname)
                    rel = os.path.relpath(fpath, tmpdir).replace("\\", "/")
                    member_name = fname
                    with open(fpath, "rb") as fh:
                        data = fh.read()
                    self._process_bytes(member_name, data,
                                        source_label=f"{label}!{rel}", depth=depth)
                    if _is_archive(member_name):
                        self._scan_archive_bytes(member_name, data, depth + 1, label)
        except OSError as exc:
            import errno
            if exc.errno == errno.ENOSPC:
                print(f"  WARNING: not enough space to extract .7z {label!r} — "
                      f"free up space in {self.temp_dir!r} or point temp_dir at a larger drive")
            else:
                print(f"  WARNING: error reading .7z {label!r}: {exc}")
        except Exception as exc:
            print(f"  WARNING: error reading .7z {label!r}: {exc}")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _scan_rar(self, path: str, depth: int, label: str) -> None:
        if not HAS_RAR:
            print("  WARNING: rarfile not installed — skipping .rar archive")
            return
        with rarfile.RarFile(path) as rf:
            for info in rf.infolist():
                if info.is_dir():
                    continue
                member_name = info.filename.split("/")[-1]
                data = rf.read(info.filename)
                self._process_bytes(member_name, data,
                                    source_label=f"{label}!{info.filename}", depth=depth)
                if _is_archive(member_name):
                    self._scan_archive_bytes(member_name, data, depth + 1, label)

    def _scan_tar(self, path: str, depth: int, label: str) -> None:
        with tarfile.open(path, "r:*") as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                member_name = member.name.split("/")[-1]
                fobj = tf.extractfile(member)
                if fobj is None:
                    continue
                data = fobj.read()
                self._process_bytes(member_name, data,
                                    source_label=f"{label}!{member.name}", depth=depth)
                if _is_archive(member_name):
                    self._scan_archive_bytes(member_name, data, depth + 1, label)

    def _scan_archive_bytes(self, name: str, data: bytes, depth: int, parent_label: str) -> None:
        if depth >= MAX_DEPTH:
            return
        suffix = _ext(name)
        os.makedirs(self.temp_dir, exist_ok=True)
        tmp_path = os.path.join(self.temp_dir, f"arc_{os.getpid()}_{id(data)}{suffix}")
        with open(tmp_path, "wb") as tmp:
            tmp.write(data)
        try:
            self._scan_archive_file(tmp_path, depth, label=f"{parent_label}!{name}")
        finally:
            os.unlink(tmp_path)

    # ── File processing ────────────────────────────────────────────────────

    def _process_file_by_hash_only(self, path: str) -> None:
        """Hash the file by MD5 and store it if it matches a manifest entry."""
        try:
            hashes = _hash_file(path)
        except Exception as exc:
            print(f"  WARNING: could not hash {path!r}: {exc}")
            return
        md5 = hashes.get("md5", "").lower()
        if not md5 or md5 not in self._md5_to_canonical:
            return  # not a known BIOS file — skip silently
        canonical = self._md5_to_canonical[md5]
        mentry = self.canonical_map.get(canonical)
        if mentry is None:
            return
        if self._should_store(canonical, hashes):
            with open(path, "rb") as fh:
                data = fh.read()
            self._store(canonical, data, hashes, mentry)
            self._hashscan_matched += 1

    def _process_file_on_disk(self, path: str, label: str) -> None:
        name = os.path.basename(path).lower()
        try:
            hashes = _hash_file(path)
        except Exception as exc:
            print(f"  WARNING: could not hash {path!r}: {exc}")
            return
        canonical, mentry = find_in_manifest(
            name, hashes, self.canonical_map, self.hash_to_canonical
        )
        if canonical is None:
            return
        if self._should_store(canonical, hashes):
            with open(path, "rb") as fh:
                data = fh.read()
            self._store(canonical, data, hashes, mentry)

    def _process_bytes(self, filename: str, data: bytes, source_label: str, depth: int) -> None:  # noqa: ARG002
        name   = filename.lower()
        hashes = _hash_bytes(data)
        canonical, mentry = find_in_manifest(
            name, hashes, self.canonical_map, self.hash_to_canonical
        )
        if canonical is None:
            return
        if self._should_store(canonical, hashes):
            self._store(canonical, data, hashes, mentry)

    def _should_store(self, canonical: str, hashes: dict) -> bool:
        new_status = determine_status(canonical, hashes, self.canonical_map)
        md5 = hashes.get("md5", "").lower()

        # If this exact MD5 blob is already stored for this canonical at the same
        # status, skip.  The check is scoped to canonical_name so that a blob
        # stored under a *different* canonical (same MD5, different name) is not
        # mistakenly treated as a duplicate — _store() needs to run in that case
        # so it can record the alias in canonical_aliases.
        if md5:
            already_md5 = self.conn.execute(
                "SELECT status FROM files WHERE md5 = ? AND canonical_name = ?",
                (md5, canonical),
            ).fetchone()
            if already_md5 and already_md5[0] == new_status:
                return False

        # Don't downgrade: if any verified variant exists and incoming isn't verified, skip.
        existing_best = get_existing_status(self.conn, canonical)
        if existing_best == "verified" and new_status != "verified":
            return False

        # For non-verified blobs, do not accumulate multiple copies per canonical.
        # Only verified blobs may coexist (regional variants with distinct MD5s).
        # A strict status upgrade (e.g. mismatch_accepted → unverifiable) is still
        # allowed; anything equal-or-worse is rejected so that duplicate unverifiable
        # or mismatch_accepted files sourced from different archives don't pile up,
        # cause bloat in the sqlar, and create path collisions during the dump stage.
        if new_status != "verified" and existing_best and existing_best != "verified":
            if STATUS_RANK.get(new_status, 99) >= STATUS_RANK.get(existing_best, 99):
                return False

        return True

    def _store(
        self,
        canonical: str,
        data: bytes,
        hashes: dict,
        manifest_entry: dict,
    ) -> None:
        ext        = _ext(canonical) or ".bin"
        sqlar_name = f"{hashes['md5']}{ext}"
        status     = determine_status(canonical, hashes, self.canonical_map)

        # If this blob is already stored under a DIFFERENT canonical (same MD5,
        # different name — e.g. scph1001.bin vs sony-playstation:dc2b9bf8…),
        # don't clobber the existing entry.  Record the alias in canonical_aliases
        # so report lookups can resolve it, then mark this canonical as found.
        already = self.conn.execute(
            "SELECT canonical_name FROM files WHERE sqlar_name = ?", (sqlar_name,)
        ).fetchone()
        if already and already[0] != canonical:
            self.conn.execute(
                "INSERT OR IGNORE INTO canonical_aliases (canonical_name, sqlar_name) "
                "VALUES (?, ?)",
                (canonical, sqlar_name),
            )
            self.conn.commit()
            self.found.add(canonical)
            return

        # Remove superseded entries on upgrade — only when the new blob is strictly
        # better status. Two verified variants with different MD5s are both kept.
        existing = self.conn.execute(
            "SELECT sqlar_name, status FROM files WHERE canonical_name = ? AND sqlar_name != ?",
            (canonical, sqlar_name),
        ).fetchall()
        had_existing = bool(existing)
        for ex_sqlar, ex_status in existing:
            if STATUS_RANK.get(status, 99) < STATUS_RANK.get(ex_status, 99):
                remove_sqlar_entry(self.conn, ex_sqlar)

        store_file(self.conn, sqlar_name, canonical, data, hashes,
                   status, manifest_entry)
        self.found.add(canonical)
        self.total_added += 1
        if had_existing and canonical in self.pre_existing:
            self.total_upgraded += 1
        _cleanup_superseded(self.conn)
        self.conn.commit()
        print(f"  [{status:20s}] {canonical}  →  {sqlar_name}")


# ---------------------------------------------------------------------------
# Build manifest (copy of research manifest + actual hashes from DB)
# ---------------------------------------------------------------------------

def write_build_manifest(
    source_manifest: dict,
    conn: sqlite3.Connection,
    json_path: str,
    csv_path: str,
) -> None:
    """
    Create combined_platform_build.json/.csv in the build/ directory.
    This is a copy of the research manifest with database_filename, size,
    and hash columns filled in from the actual stored files.
    The source research manifest is NOT modified.
    """
    import copy
    build_manifest = copy.deepcopy(source_manifest)
    build_manifest["generated_at"] = datetime.now(timezone.utc).isoformat()

    rows = conn.execute(
        "SELECT canonical_name, sqlar_name, sha1, md5, sha256, crc32, size "
        "FROM files WHERE status != 'missing'"
    ).fetchall()

    for canonical, sqlar_name, sha1, md5, sha256, crc32, size in rows:
        if canonical in build_manifest["files"]:
            fd = build_manifest["files"][canonical]
            fd["database_filename"] = sqlar_name
            fd["sha1"]   = sha1
            fd["md5"]    = md5
            fd["sha256"] = sha256
            fd["crc32"]  = crc32
            fd["size"]   = size

    # Also update alias canonicals — their bytes are stored under a different
    # canonical_name, so they have no direct files row. Pull hashes from the
    # primary entry via canonical_aliases.
    alias_rows = conn.execute(
        "SELECT ca.canonical_name, f.sqlar_name, f.sha1, f.md5, f.sha256, f.crc32, f.size "
        "FROM canonical_aliases ca "
        "JOIN files f ON f.sqlar_name = ca.sqlar_name"
    ).fetchall()
    for canonical, sqlar_name, sha1, md5, sha256, crc32, size in alias_rows:
        if canonical in build_manifest["files"]:
            fd = build_manifest["files"][canonical]
            fd["database_filename"] = sqlar_name
            fd["sha1"]   = sha1
            fd["md5"]    = md5
            fd["sha256"] = sha256
            fd["crc32"]  = crc32
            fd["size"]   = size

    # Write JSON
    Path(json_path).parent.mkdir(parents=True, exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(build_manifest, fh, indent=2, ensure_ascii=False)
    print(f"  Build manifest JSON → {json_path}")

    # Write CSV
    _write_csv(build_manifest, csv_path)
    print(f"  Build manifest CSV  → {csv_path}")


def _write_csv(manifest: dict, path: str) -> None:
    headers = ["database_filename", "size", "sha1", "md5", "sha256", "crc32"]
    for p in PLATFORMS:
        headers += [f"{p}_known_file", f"{p}_aliases",
                    f"{p}_staging_path", f"{p}_expected_hashes"]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=headers)
        writer.writeheader()
        for _canonical, fdata in manifest["files"].items():
            row: dict[str, Any] = {
                "database_filename": fdata["database_filename"],
                "size":   "unknown" if fdata.get("size") is None else str(fdata["size"]),
                "sha1":   fdata.get("sha1")   or "unknown",
                "md5":    fdata.get("md5")    or "unknown",
                "sha256": fdata.get("sha256") or "unknown",
                "crc32":  fdata.get("crc32")  or "unknown",
            }
            for p in PLATFORMS:
                pdata = (fdata.get("platforms") or {}).get(p) or {}
                if not pdata.get("known_file"):
                    row[f"{p}_known_file"]      = "not present"
                    row[f"{p}_aliases"]         = "not present"
                    row[f"{p}_staging_path"]    = "not present"
                    row[f"{p}_expected_hashes"] = "not present"
                else:
                    row[f"{p}_known_file"] = "Yes"
                    staging = pdata.get("staging_paths") or []
                    filenames = list(dict.fromkeys(s.split("/")[-1] for s in staging if s))
                    row[f"{p}_aliases"] = ",".join(filenames) if filenames else "none"
                    staging = pdata.get("staging_paths") or []
                    row[f"{p}_staging_path"] = ",".join(staging)
                    hash_parts: list[str] = []
                    for ht in HASH_TYPES:
                        for hv in (pdata.get("expected_hashes") or {}).get(ht, []):
                            hash_parts.append(f"{ht}:{hv}")
                    row[f"{p}_expected_hashes"] = ",".join(hash_parts) if hash_parts else "unverifiable"
            writer.writerow(row)


# ---------------------------------------------------------------------------
# Missing files
# ---------------------------------------------------------------------------

def _canonical_in_db(conn: sqlite3.Connection, canonical: str, fdata: dict) -> bool:
    """
    Return True if the physical content for *canonical* is present in the DB —
    either stored directly under that canonical_name, or under an alias canonical
    that shares one of the expected hashes.  Used by populate_missing_files so
    that alias files (e.g. scph1001.bin vs sony-playstation:dc2b9bf8…) are not
    incorrectly added to the shopping list when their content is already stored.
    """
    row = conn.execute(
        "SELECT 1 FROM files WHERE canonical_name = ?", (canonical,)
    ).fetchone()
    if row:
        return True
    for p in PLATFORMS:
        pinfo = (fdata.get("platforms") or {}).get(p) or {}
        for ht in HASH_TYPES:
            for hv in (pinfo.get("expected_hashes") or {}).get(ht, []):
                if not hv:
                    continue
                hit = conn.execute(
                    f"SELECT 1 FROM files WHERE {ht} = ?", (hv.lower(),)
                ).fetchone()
                if hit:
                    return True
    # Also check canonical_aliases — covers alias canonicals whose bytes are stored
    # under a different canonical_name with no declared-hash overlap.
    alias = conn.execute(
        "SELECT 1 FROM canonical_aliases WHERE canonical_name = ?", (canonical,)
    ).fetchone()
    if alias:
        return True
    return False


def populate_missing_files(
    conn: sqlite3.Connection, canonical_map: dict, found: set[str]
) -> None:
    conn.execute("DELETE FROM missing_files")
    for canonical, fdata in canonical_map.items():
        if canonical in found:
            continue
        if _canonical_in_db(conn, canonical, fdata):
            continue
        for p in PLATFORMS:
            pinfo = (fdata.get("platforms") or {}).get(p) or {}
            if not pinfo.get("known_file"):
                continue
            conn.execute(
                "INSERT OR IGNORE INTO missing_files "
                "(canonical_name, system, platform, required, expected_hashes) "
                "VALUES (?,?,?,?,?)",
                (
                    canonical, "unknown", p,
                    1 if pinfo.get("required") else 0,
                    json.dumps(pinfo.get("expected_hashes") or {}),
                ),
            )
    conn.commit()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _manage_sources(
    sources: list[str],
    config: configparser.ConfigParser,
    base_dir: str,
) -> list[str]:
    """
    Interactive source management prompt shown before each build.
    Lets the user review, add, edit, or remove sources, then saves any
    changes to bios_preservation_user.conf before returning the final list.
    """
    user_conf_path = str(Path(base_dir) / "configure" / "bios_preservation_user.conf")

    def _save_sources(src_list: list[str]) -> None:
        """Merge updated sources into the user conf file."""
        cfg = configparser.ConfigParser()
        if os.path.exists(user_conf_path):
            cfg.read(user_conf_path, encoding="utf-8")
        if not cfg.has_section("build"):
            cfg.add_section("build")
        # Remove all existing source_N keys in [build]
        for key in list(cfg.options("build")):
            if key.startswith("source_"):
                cfg.remove_option("build", key)
        # Write current list
        for idx, src in enumerate(src_list, start=1):
            cfg.set("build", f"source_{idx}", src)
        Path(user_conf_path).parent.mkdir(parents=True, exist_ok=True)
        with open(user_conf_path, "w", encoding="utf-8") as fh:
            fh.write("# bios_preservation_user.conf\n")
            fh.write("# Generated by bios_configure.py / bios_build.py\n")
            fh.write("# Delete this file to restore defaults.\n\n")
            cfg.write(fh)
        print(f"  Sources saved → {user_conf_path}")

    while True:
        print(f"\n{'='*60}")
        print("  BUILD — Source Configuration")
        print("─" * 60)
        if sources:
            print("  Current sources:")
            for i, s in enumerate(sources, start=1):
                # Strip legacy hashscan: prefix for display
                label = s[9:] if s.startswith("hashscan:") else s
                print(f"    {i}. {label}")
        else:
            print("  Current sources:  (none configured)")
        print()
        print("  [A] Add a source (path, URL, or archive)")
        print("  [E] Edit / remove a source")
        print("  [C] Continue with current sources")
        print("─" * 60)

        choice = input("  Enter choice [A/E/C]: ").strip().upper()

        if choice == "C":
            break

        elif choice == "A":
            new_src = input("  Enter path or URL: ").strip()
            if not new_src:
                print("  No input — source not added.")
                continue
            # Resolve local paths; leave URLs as-is
            if not new_src.lower().startswith(("http://", "https://")):
                new_src = _resolve(new_src, base_dir)
            sources.append(new_src)
            _save_sources(sources)

        elif choice == "E":
            if not sources:
                print("  No sources to edit.")
                continue
            print("\n  Select a source to edit or remove:")
            for i, s in enumerate(sources, start=1):
                label = s[9:] if s.startswith("hashscan:") else s
                print(f"    {i}. {label}")
            raw = input("  Enter number (or Enter to cancel): ").strip()
            if not raw:
                continue
            if not raw.isdigit() or not (1 <= int(raw) <= len(sources)):
                print("  Invalid selection.")
                continue
            idx = int(raw) - 1
            print(f"\n  Selected: {sources[idx]}")
            print("  [E] Edit   [D] Delete   [Enter] Cancel")
            action = input("  Choice [E/D]: ").strip().upper()
            if action == "D":
                removed = sources.pop(idx)
                print(f"  Removed: {removed}")
                _save_sources(sources)
            elif action == "E":
                new_val = input(f"  New value (current: {sources[idx]}): ").strip()
                if new_val:
                    if not new_val.lower().startswith(("http://", "https://")):
                        new_val = _resolve(new_val, base_dir)
                    sources[idx] = new_val
                    _save_sources(sources)
                else:
                    print("  No input — source unchanged.")
        else:
            print("  Invalid choice. Please enter A, E, or C.")

    return sources


def run(config: configparser.ConfigParser, base_dir: str = ".") -> bool:
    section = "build"

    manifest_input = _resolve(
        config.get(section, "manifest_input",
                   fallback="update/combined_platform_manifest.json"),
        base_dir,
    )
    sqlar_output = _resolve(
        config.get(section, "sqlar_output", fallback="build/bios_database.sqlar"),
        base_dir,
    )
    json_output = _resolve(
        config.get(section, "json_output",
                   fallback="build/combined_platform_build.json"),
        base_dir,
    )
    csv_output = _resolve(
        config.get(section, "csv_output",
                   fallback="build/combined_platform_build.csv"),
        base_dir,
    )
    incremental = config.getboolean(section, "incremental", fallback=True)
    temp_dir    = _resolve(config.get(section, "temp_dir", fallback="temp"), base_dir)
    os.makedirs(temp_dir, exist_ok=True)

    sources: list[str] = []
    i = 1
    while True:
        key = f"source_{i}"
        if config.has_option(section, key):
            src = config.get(section, key).strip()
            if src:
                # Resolve local paths; leave URLs as-is
                if not src.lower().startswith(("http://", "https://")):
                    src = _resolve(src, base_dir)
                sources.append(src)
            i += 1
        else:
            break

    # ── Interactive source review ──────────────────────────────────────────
    sources = _manage_sources(sources, config, base_dir)

    # ── Load research manifest (read-only) ────────────────────────────────
    print(f"[build] Loading update manifest from {manifest_input!r} …")
    if not os.path.exists(manifest_input):
        print(f"ERROR: manifest not found: {manifest_input}")
        return False

    with open(manifest_input, "r", encoding="utf-8") as fh:
        source_manifest: dict = json.load(fh)

    canonical_map, hash_to_canonical, md5_to_canonical = build_lookups(source_manifest)
    manifest_generated_at: str = source_manifest.get("generated_at", "")
    print(f"  {len(canonical_map)} canonical files in update manifest")

    # ── Open / init sqlar database ─────────────────────────────────────────
    if not incremental and os.path.exists(sqlar_output):
        print(f"[build] Incremental=false — removing existing {sqlar_output!r}")
        os.unlink(sqlar_output)

    Path(sqlar_output).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(sqlar_output)
    conn.execute("PRAGMA page_size = 4096")
    init_db(conn)

    # ── Purge orphans on every build (manifest-agnostic safety net) ────────
    orphan_count = _purge_orphans(conn, canonical_map)
    if orphan_count:
        print(f"[build] Purged {orphan_count} orphaned blob(s) not in current manifest.")

    # ── Full audit if research manifest was regenerated ────────────────────
    stored_ts = conn.execute(
        "SELECT value FROM meta WHERE key = 'manifest_generated_at'"
    ).fetchone()
    if stored_ts and stored_ts[0] != manifest_generated_at:
        print("[build] Research manifest has changed — auditing sqlar …")
        audit_sqlar(conn, canonical_map)

    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('manifest_generated_at', ?)",
        (manifest_generated_at,),
    )
    conn.commit()

    # ── Scan sources ───────────────────────────────────────────────────────
    if not sources:
        print("[build] WARNING: no sources configured.")
    else:
        print(f"[build] Scanning {len(sources)} source(s) …")

    found: set[str] = set(
        row[0] for row in conn.execute("SELECT canonical_name FROM files")
    )

    scanner = Scanner(conn, canonical_map, hash_to_canonical, md5_to_canonical, found, temp_dir)
    for src in sources:
        print(f"\n  Source: {src!r}")
        scanner.scan_source(src)

    new_count = scanner.total_added - scanner.total_upgraded
    print(f"\n[build] Scan complete.  "
          f"Stored {scanner.total_added} blob(s)  "
          f"({new_count} new, {scanner.total_upgraded} upgraded).")
    if scanner.total_added > 0:
        print(f"         Note: blob count may exceed canonical count when multiple verified")
        print(f"         variants of the same file are stored (e.g. regional ROM versions).")

    # ── Populate missing_files ─────────────────────────────────────────────
    all_found = scanner.found | found
    populate_missing_files(conn, canonical_map, all_found)
    missing_count = conn.execute(
        "SELECT COUNT(DISTINCT canonical_name) FROM missing_files"
    ).fetchone()[0]

    # ── Collection statistics ──────────────────────────────────────────────
    # Canonical-level counts: a canonical is counted by its best-status blob.
    canonical_status: dict[str, str] = {}
    for _sqlar, canonical, status in conn.execute(
        "SELECT sqlar_name, canonical_name, status FROM files"
    ):
        existing = canonical_status.get(canonical)
        if existing is None or STATUS_RANK.get(status, 99) < STATUS_RANK.get(existing, 99):
            canonical_status[canonical] = status

    db_verified     = sum(1 for s in canonical_status.values() if s == "verified")
    db_unverifiable = sum(1 for s in canonical_status.values() if s == "unverifiable")
    db_mismatch     = sum(1 for s in canonical_status.values() if s == "mismatch_accepted")
    db_present      = len(canonical_status)
    # Alias canonicals live only in canonical_aliases — they have no files row and
    # are excluded from missing_files by _canonical_in_db().  Count them separately
    # so db_total matches the manifest.
    alias_count = conn.execute(
        "SELECT COUNT(DISTINCT canonical_name) FROM canonical_aliases"
    ).fetchone()[0]
    db_total        = db_present + missing_count + alias_count

    # Blob-level counts
    total_blobs    = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    verified_blobs = conn.execute(
        "SELECT COUNT(*) FROM files WHERE status = 'verified'"
    ).fetchone()[0]
    multi_variant  = conn.execute(
        "SELECT COUNT(*) FROM ("
        "  SELECT canonical_name FROM files WHERE status = 'verified'"
        "  GROUP BY canonical_name HAVING COUNT(*) > 1"
        ")"
    ).fetchone()[0]

    # ── Print definitions then summary ─────────────────────────────────────
    print(f"\n[build] Definitions:")
    print(f"  canonical  — one unique BIOS file identity across all platforms.")
    print(f"               Two platforms naming the same bytes differently = one canonical.")
    print(f"  blob       — one physical binary stored in the database.")
    print(f"               A canonical may have multiple blobs when platforms accept")
    print(f"               different regional variants (each variant = separate blob).")
    print(f"  present    — at least one blob is stored for this canonical.")
    print(f"  missing    — no blob found in any scanned source yet.")
    print(f"  verified   — a stored blob's hash matches a declared hash for this canonical.")
    print(f"  unverifiable — stored, but no platform declares a hash to check against.")
    print(f"  hash mismatch — stored, but the blob's hash matches none of the declared values.")

    print(f"\n[build] Collection summary — {db_total} canonical(s) across all platforms:")
    print(f"  Present  : {db_present:>6}  (at least one blob stored)")
    print(f"    verified          : {db_verified:>6}")
    print(f"    unverifiable      : {db_unverifiable:>6}")
    print(f"    hash mismatch     : {db_mismatch:>6}")
    if alias_count:
        print(f"    via alias         : {alias_count:>6}  (bytes stored under a different canonical name)")
    print(f"  Missing  : {missing_count:>6}  (not yet found in any source, across all platforms)")
    print(f"\n  Blobs stored : {total_blobs} total  "
          f"({verified_blobs} verified"
          f"{f', {multi_variant} canonical(s) with multiple verified variants' if multi_variant else ''})")

    sl_total = missing_count + db_mismatch + db_unverifiable
    print(f"\n  Report counts: each platform's 'PHYSICAL FILES' line uses these same")
    print(f"  definitions. Per-platform missing counts will be lower than {missing_count}")
    print(f"  because each platform only declares a subset of all canonicals.")
    print(f"\n  Shopping list: roughly {sl_total} rows expected "
          f"({missing_count} missing + {db_mismatch} mismatch + {db_unverifiable} unverifiable).")
    print(f"  Actual row count varies: mismatch files expand when multiple MD5 variants")
    print(f"  are declared; missing files may consolidate when canonicals share an expected MD5.")
    print(f"  If the shopping list appears empty or stale, re-run Build then Report.")

    # ── Write build output files (research manifest stays untouched) ───────
    print("\n[build] Writing build manifests …")
    write_build_manifest(source_manifest, conn, json_output, csv_output)

    conn.close()
    print(f"[build] Done.  Database: {sqlar_output!r}")
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
