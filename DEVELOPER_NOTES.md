# Developer Notes — BIOS Preservation Tool

Technical reference for contributors and anyone extending the tool. Assumes familiarity with the user-facing README.

---

## Table of Contents

1. [Database Schema](#database-schema)
2. [File Identity Model](#file-identity-model)
3. [Manifest JSON Structure](#manifest-json-structure)
4. [Source YAML Structure](#source-yaml-structure)
5. [CSV Column Reference](#csv-column-reference)
6. [Implementation Notes](#implementation-notes)

---

## Database Schema

The database is a standard SQLite file using the [sqlar](https://sqlite.org/sqlar.html) schema for blob storage, extended with additional tables for file tracking.

### `sqlar` — blob storage

```sql
CREATE TABLE sqlar (
    name  TEXT PRIMARY KEY,   -- storage key: "{md5}.{ext}", e.g. "a5c85cf57b56a98e.bin"
    mode  INT,                -- Unix file mode (always 0)
    mtime INT,                -- modification time (always 0)
    sz    INT,                -- uncompressed size in bytes
    data  BLOB                -- raw file content (uncompressed)
);
```

### `files` — canonical file registry

```sql
CREATE TABLE files (
    sqlar_name      TEXT PRIMARY KEY,  -- references sqlar.name
    canonical_name  TEXT NOT NULL,     -- lowercase canonical identifier
    status          TEXT NOT NULL,     -- "verified" | "unverifiable" | "mismatch_accepted"
    sha1            TEXT,
    md5             TEXT,
    sha256          TEXT,
    crc32           TEXT,
    size            INTEGER
);
```

A canonical may have multiple rows in `files` (one per verified regional variant). The `sqlar_name` primary key is always an MD5-based filename — two variants with different MD5s coexist naturally.

### `missing_files` — canonicals not yet found

```sql
CREATE TABLE missing_files (
    canonical_name TEXT PRIMARY KEY
);
```

Populated at the end of each build run for canonicals present in the manifest but absent from `files`. Cleared and repopulated on every build. Alias canonicals are excluded by `_canonical_in_db()` — they are not in `files` but their bytes are present, so they are not missing. As a consequence, alias canonicals appear in neither `files` nor `missing_files`, and `db_total = db_present + missing_count` would undercount them. `alias_count` (from `COUNT(DISTINCT canonical_name) FROM canonical_aliases`) is added to `db_total` and reported as a `via alias` line in the build summary.

`_canonical_in_db()` returns `True` when a canonical's content is already present — either directly in `files` or via a matching hash — preventing it from being added to `missing_files`. Alias registration for canonicals not encountered during scanning is handled by `reconcile_aliases()`, which runs after `populate_missing_files` (see Note 17). Alias canonicals with no declared hashes (unverifiable) are covered by `bios_restore.py`'s `.aliases.json` sidecar handling on restore.

### `canonical_aliases` — alias resolution

```sql
CREATE TABLE canonical_aliases (
    canonical_name  TEXT NOT NULL,
    sqlar_name      TEXT NOT NULL,
    PRIMARY KEY (canonical_name, sqlar_name)
);
```

Populated when `_store()` detects that the same blob (same MD5) is already stored under a different `canonical_name`. The incoming canonical is recorded here so report lookups can resolve it without creating a duplicate `files` row.

### `meta` — build metadata

```sql
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
```

Stores `generated_at` (ISO 8601 timestamp of the last manifest generation) and `unrar_tool` (path to the UnRAR binary, if saved). The `generated_at` value is used to detect manifest regeneration between build runs and trigger an orphan purge.

---

## File Identity Model

**Canonical** — the lowercase filename used as the logical identity of a BIOS file across all platforms. Derived from the first `name:` field encountered for a given hash identity in the manifest. A canonical may map to many different filenames across platforms (e.g. `scph5500.bin` and `sony-playstation:8d8cb7fe...`).

**Blob** — a row in `files` + the corresponding row in `sqlar`. Keyed by `{md5}.{ext}`. Multiple blobs can exist for one canonical when platforms declare different accepted MD5s (regional variants). All are stored; staging picks the best match for each platform.

**Alias canonical** — a canonical whose physical bytes are already stored under a different `canonical_name`. Recorded in `canonical_aliases`. Two sub-cases:

- **Hash-resolvable** — the canonical has declared hashes that match the stored blob. `reconcile_aliases()` finds and registers these in its post-scan pass (see Note 17).
- **Unverifiable** — no declared hashes. Registered only when a `.aliases.json` sidecar is present in a scanned zip, or when `_store()` detects the same-MD5 collision during an original source scan.

The report step uses `canonical_aliases` as a 4th fallback lookup.

### `_get_file_rows()` lookup chain (bios_report.py)

1. Direct `canonical_name` lookup in `files`
2. Hash-based fallback — query `files` for any declared hash from the platform manifest
3. `database_filename` fallback — query `files.sqlar_name` using the manifest's stored filename field
4. `canonical_aliases` fallback — resolve via the alias table

### Status hierarchy

```
verified  >  unverifiable  >  mismatch_accepted
```

Status only ever upgrades. When a higher-status blob supersedes a lower-status one, the lower blob is deleted from `sqlar` and `files`. Two `verified` blobs for the same canonical (different MD5s) coexist — neither supersedes the other.

---

## Manifest JSON Structure

`combined_platform_manifest.json` (Update output) and `combined_platform_build.json` (Build output) share the same structure. Build adds real `database_filename` values; Update uses zero-padded integer placeholders.

```json
{
  "generated_at": "2026-03-25T17:22:00Z",
  "files": {
    "scph5500.bin": {
      "database_filename": "8dd7d5296a650fac7319bce665a6a53c.bin",
      "size": 524288,
      "sha1": "9c0421858e217805f4abe18698afea8d5aa36ff7",
      "md5": "8dd7d5296a650fac7319bce665a6a53c",
      "sha256": null,
      "crc32": "37157331",
      "platforms": {
        "batocera": {
          "known_file": true,
          "staging_paths": ["scph5500.bin"],
          "expected_hashes": {
            "md5":    ["8dd7d5296a650fac7319bce665a6a53c"],
            "sha1":   ["9c0421858e217805f4abe18698afea8d5aa36ff7"],
            "sha256": [],
            "crc32":  ["37157331"]
          },
          "expected_size": 524288
        },
        "retrodeck": {
          "known_file": true,
          "staging_paths": ["scph5500.bin"],
          "expected_hashes": { ... }
        }
      }
    }
  },
  "platform_metadata": {
    "batocera": {
      "base_destination": "bios"
    }
  }
}
```

Key points:
- Top-level keys are lowercase canonical names
- `database_filename` is `{md5}.{ext}` after Build, or a zero-padded placeholder before
- `platforms` only contains entries for platforms that declare this file (`known_file: true`)
- `expected_hashes` is always a dict of lists, even for single values
- `platform_metadata.base_destination` is used by Stage to construct the full output path

---

## Source YAML Structure

Platform YAML files from Abdess/retrobios follow this schema:

```yaml
platform:          "Batocera"
base_destination:  "bios"        # root staging path for this platform
hash_type:         "md5"
verification_mode: "md5"

systems:
  sony-playstation:
    files:
      - name:        scph5500.bin      # filename the platform uses (case-preserved)
        destination: scph5500.bin      # path relative to base_destination
        required:    true
        md5:         8dd7d5296a650fac7319bce665a6a53c
        sha1:        9c0421858e217805f4abe18698afea8d5aa36ff7
        crc32:       37157331
        size:        524288             # bytes, optional
```

Key schema details:
- `name` is what the platform calls the file (case-sensitive, always preserved in output)
- `destination` is the path relative to `base_destination` (may include subdirectories, e.g. `np2kai/bios.rom`)
- Hash fields (`md5`, `sha1`, `sha256`, `crc32`) are optional. Multiple accepted values are comma-separated in a single string
- `size` in bytes is optional
- `inherits: parent_platform` — child declarations take precedence over parent's per filename
- `includes: [group_name]` — expands shared file groups from `_shared.yml`

### Shared groups (`_shared.yml`)

Defines file groups used across multiple platforms and systems. Groups use a bare list (no `files:` wrapper):

```yaml
shared_groups:
  np2kai:
    - name: bios.rom
      destination: np2kai/bios.rom
      required: true
      md5: cd237e16e7e77c06bb58540e9e9fca68
```

The subdirectory a BIOS goes into is determined by the libretro core, not the platform. Shared groups encode the correct destination once so platforms cannot drift. Examples: `np2kai`, `keropi`, `quasi88`, `fuse`, `kronos`, `ep128emu`, `mt32`, `jiffydos`.

---

## CSV Column Reference

### `combined_platform_manifest.csv` / `combined_platform_build.csv`

One row per canonical. Columns:

| Column | Description |
|--------|-------------|
| `database_filename` | `{md5}.{ext}` (build) or zero-padded placeholder (update) |
| `size` | Declared size in bytes, or `unknown` |
| `sha1` | Best known SHA1, or `unknown` |
| `md5` | Best known MD5, or `unknown` |
| `sha256` | Best known SHA256, or `unknown` |
| `crc32` | Best known CRC32, or `unknown` |
| `{platform}_known_file` | `Yes` or `not present` |
| `{platform}_aliases` | Comma-separated staging-path basenames |
| `{platform}_staging_path` | Comma-separated full staging paths |
| `{platform}_expected_hashes` | `ht:value,ht:value,...` or `unverifiable` |

Repeated 4× for each platform (40 platform columns for 10 platforms, plus 6 database columns = 46 total).

### `report/<platform>_report.csv`

One row per staging path, expanded per declared MD5 variant when mismatched. Three comment header lines precede the data:

```
# PHYSICAL FILES (matches build): total=X present=Y verified=A unverifiable=B hash_mismatch=C missing=D
# MANIFEST ENTRIES: total=X present=Y missing=Z hash_mismatch=W
# STAGING PATHS: total=X present=Y missing=Z (counts reflect unique staging paths; actual CSV rows may be higher ...)
```

Data columns: `filename`, `present`, `staging_path`, `actual size`, `expected size`, `actual sha1`, `expected sha1`, `actual md5`, `expected md5`, `actual sha256`, `expected sha256`, `actual crc32`, `expected crc32`.

### `report/global_shopping_list.csv`

One row per distinct declared MD5 that is not yet stored as verified.

| Column | Description |
|--------|-------------|
| `Known Aliases` | Comma-sorted staging-path basenames; canonical added if not already present (case-insensitive) |
| `Expected MD5` | The MD5 to search for, or `unknown` if no MD5 declared |
| `Status` | `missing` / `hash_mismatch` / `unverifiable` |
| `Platforms` | Comma-sorted platform display names |
| `Actual MD5` | MD5 of what is currently stored, or `not present` |

### `report/shopping_missing.csv` / `shopping_hash_mismatch.csv` / `shopping_unverifiable.csv`

Pre-filtered subsets of `global_shopping_list.csv`, one file per status value. Written in the same Report run, immediately after the global list. Schema is identical — same five columns, same sort order (canonical name). Intended for focused triage: hand the mismatch file to someone hunting wrong-version files, the missing file to someone assembling a collection from scratch, and so on.

---

## Implementation Notes

### 1. Filename normalisation

All filename comparisons are performed on lowercase-normalised strings internally. All output (CSV, JSON, staged filenames) preserves original case exactly as declared in the YAML source.

### 2. Storage key convention

The `{md5}.{ext}` storage convention in the sqlar eliminates case-sensitivity ambiguity and duplicate-filename collisions at the storage layer. The extension is taken from the canonical name; `.bin` is used as a fallback when the canonical has no extension.

### 3. Inheritance resolution

When a platform YAML uses `inherits:`, the parent is fully resolved before the child is processed. The child's declarations take precedence over the parent's for the same filename (matched case-insensitively). Circular inheritance is detected and stopped with a warning.

### 4. `_data_dirs.yml` is out of scope

Asset data directories (Dolphin/PPSSPP/blueMSX game data packs) are defined in `_data_dirs.yml` in the upstream project. This tool processes only individual BIOS files, not data directories.

### 5. Status upgrade path

Status rank: `verified (1) > unverifiable (2) > mismatch_accepted (3)`. A lower rank number is better. Status only ever moves to a lower number. A `mismatch_accepted` blob is deleted when any `verified` blob for the same canonical is stored.

This protection applies equally to alias canonicals. `get_existing_status()` falls through to `canonical_aliases` when `files` has no row for the canonical, so a canonical that is effectively verified via an alias is treated as verified for downgrade-protection purposes.

**Alias registration also triggers stale-blob cleanup.** When `_store()` detects that an incoming blob already exists in `sqlar` under a different `canonical_name` (the same-MD5 alias case), it registers the alias in `canonical_aliases` and then checks whether the incoming canonical still has any direct `files` entries with lower status than the alias target's status. If so, those stale blobs are removed via `remove_sqlar_entry()`. This covers the scan-order scenario where canonicals sharing an expected MD5 (e.g. `syscard3.pce` and `syscard3u.pce`) were scanned in a previous session, leaving a `mismatch_accepted` blob for the secondary canonical even after the correct version was stored for the primary. Without this cleanup, the collection summary would continue counting those canonicals as `hash_mismatch` even though their bytes are present and verified via the alias.

### 6. Multi-variant coexistence

Two `verified` blobs for the same canonical (different MD5s) coexist without one superseding the other. `_cleanup_superseded()` only removes `mismatch_accepted` blobs when a `verified` copy exists — it never removes a `verified` blob to make room for another `verified` blob.

Non-verified blobs (`unverifiable`, `mismatch_accepted`) are limited to one per canonical. If a second non-verified blob arrives for the same canonical, `_should_store()` rejects it unless it is a strict status upgrade (e.g. `mismatch_accepted` → `unverifiable`). This constraint prevents duplicate blobs with the same canonical name from colliding in the dump zip and from causing false inflation in collection counts.

### 7. Pre-existing snapshot for upgrade counting

`Scanner.__init__` takes a `pre_existing` snapshot (`frozenset(found)`) before scanning begins. The `total_upgraded` counter is only incremented when the canonical was in `pre_existing`. This prevents pass 1 → pass 2 within-run promotions from being counted as cross-run upgrades.

### 8. `_should_store()` logic

Three conditions must all pass for a blob to be stored:
1. This exact MD5 is not already stored **for this canonical** with the same status. The check is scoped to `canonical_name` — if the same MD5 is already stored under a *different* canonical, condition 1 passes so that `_store()` can run and register the alias in `canonical_aliases`. An unscoped check would silently block alias registration for any file whose bytes are already present under another name.
2. The incoming status is not lower than the best existing status for this canonical (no downgrading a verified canonical with unverifiable/mismatch data). "Existing status" is determined by `get_existing_status()`, which checks `files` first and falls back to `canonical_aliases` — so alias canonicals are protected even though they have no direct row in `files`.
3. For non-verified blobs: no blob of equal or better non-verified status already exists for this canonical. Only `verified` blobs may coexist (regional variants with distinct MD5s). A strict status upgrade (e.g. `mismatch_accepted` → `unverifiable`) is allowed; a same-status or lower-status duplicate is rejected. This prevents duplicate `unverifiable` or `mismatch_accepted` blobs from accumulating across multiple source scans, which would cause sqlar bloat and path collisions during the dump stage.

### 9. Source management and persistence

Source paths are managed interactively at the start of each Build run and persisted to `bios_preservation_user.conf` automatically as `source_1`, `source_2`, … `source_N`. They can also be set manually in the conf file.

### 10. Manifest regeneration detection

The build step reads `generated_at` from the `meta` table and compares it to the timestamp in the incoming manifest. If they differ, the manifest has been regenerated and `_purge_orphans()` is run before scanning — removing blobs whose `canonical_name` is no longer present in the current manifest.

### 11. Backup naming

Backup files: `DD_mon_YYYY_backup.zip`. Cross-platform safe. If a file with today's date already exists, a counter suffix is appended (`(2)`, `(3)`, …). No existing file is ever overwritten.

### 11a. Backup sidecars (`.blob_map.json` and `.aliases.json`)

The backup zip contains two sidecars written by `bios_backup.py`:

**`.blob_map.json`** — maps every `sqlar_name → {canonical_name, status}` for all blobs in the `files` table. `bios_restore.py` uses this to identify each blob by its canonical name directly, without relying on the current manifest. This makes restoration robust when the manifest has changed since the backup was made — verified blobs are named `{md5}.{ext}` in the backup and could not otherwise be matched back to a canonical if their MD5 is no longer declared in the manifest.

**`.aliases.json`** — records every `(canonical_name, sqlar_name)` pair from `canonical_aliases`. Alias canonicals have no row in `files` and no blob of their own — they would otherwise be lost entirely during restore. `bios_restore.py` re-inserts each alias pair, but only if the target blob was actually re-ingested (exists in `sqlar`). This guard prevents alias entries from a backup of database A being injected into an unrelated database B.

### 12. 7z extraction and temp directory

py7zr 1.x removed the `read()` API. The tool uses `extractall()` to a subdirectory inside `temp/` for all `.7z` scanning. This avoids the RAM-backed `/tmp` filesystem on Linux (often capped at 50% of RAM). The temp directory is always cleaned up in a `finally` block. On out-of-space errors the error message explicitly names `temp_dir` as the setting to change.

### 13. YAML cache directory naming

The cache directory is named `yaml_cache/` rather than `yaml/`. Python treats any folder on `sys.path` as a potential package — a folder named `yaml/` in the project root would shadow the PyYAML library, causing `AttributeError: module 'yaml' has no attribute 'safe_load'`. Any existing installations with a `yaml/` folder should rename it and update `yaml_local_dir` and `yaml_cache_dir` in `bios_preservation.conf`.

### 14. Alias canonical lookup

When `_store()` finds that the incoming blob's MD5 already exists in `sqlar` under a different `canonical_name`, it records the new name in `canonical_aliases` (rather than overwriting the existing entry). For this to happen, `_should_store()` must first return `True` — which is why its MD5 duplicate check (condition 1) is scoped to the same `canonical_name`. Without that scope, a file matched by filename to canonical B whose bytes are already stored under canonical A would be silently dropped by `_should_store()`, and the alias would never be recorded. `_get_file_rows()` in `bios_report.py` uses `canonical_aliases` as its 4th fallback lookup, after direct canonical name, hash-based, and `database_filename` lookups all fail. `write_build_manifest()` also joins against `canonical_aliases` to fill `database_filename` for alias canonicals.

The `canonical_aliases` table is populated in three ways:

1. **`_store()` during scanning** — when an incoming blob’s MD5 matches an existing `sqlar` entry under a different canonical, the new name is inserted as an alias. After registering the alias, `_store()` also removes any stale lower-status direct blobs the incoming canonical had in `files` (see Note 5 for detail on this cleanup).
2. **`reconcile_aliases()` post-scan pass** — runs after `populate_missing_files` on every build. Handles canonicals whose bytes were never directly encountered during scanning but whose declared MD5 is already stored under a different canonical, and cleans up stale `mismatch_accepted` and `unverifiable` blobs superseded by a verified alias. See Note 17 for full detail.
3. **`bios_restore.py` via `.aliases.json` sidecar** — re-inserts alias pairs recorded by `bios_backup.py`, but only for pairs whose target blob was actually re-ingested. Handles unverifiable alias canonicals that have no declared hashes and cannot be found by any hash lookup.

On a fresh database built from original sources, the table is populated by paths 1 and 2. A full rebuild (`incremental = false`) guarantees complete population. When building from a backup zip, path 3 handles the cases paths 1 and 2 cannot reach.

`get_existing_status()` checks `files` first, then falls back to `canonical_aliases` (joining against `files` on `sqlar_name` to retrieve the target blob's status). This ensures that `_should_store()`'s downgrade protection fires correctly for alias canonicals — without this fallback, any alias canonical would appear to have no existing status, allowing wrong-version files to be stored as `mismatch_accepted` for canonicals that are already effectively verified via an alias.

### 15. Shopping list status determination

`_sl_status_for_platform()` re-evaluates status for each (canonical, platform) pair using only that platform's declared hashes — not the global DB status stored at scan time. This ensures that a file verified by Platform A but undeclared by Platform B correctly appears as `unverifiable` from Platform B's perspective.

The shopping list uses a two-bucket accumulation per canonical across all enabled platforms:

- `per_md5` — one entry per distinct declared MD5 that didn't match. Emitted regardless of `any_verified`. Already-verified MD5s (present in `verified_md5s`) are skipped.
- `no_md5` — collects platforms that declare no MD5. Emitted only when `any_verified = False` **and** `per_md5` is empty.

When a platform declares non-MD5 hashes only (e.g. SHA1 but no MD5), and those hashes don't match, the status is converted from `hash_mismatch` to `unverifiable` before entering the `no_md5` bucket — there is no MD5 to express what to search for.

A final sanity pass before CSV write corrects any `unverifiable + known expected MD5` combination to `hash_mismatch` (the two states are mutually exclusive by definition).

### 16. Report row expansion for multi-variant mismatches

When a file is present but has multiple declared MD5 variants and none match what is stored, the per-platform report emits one row per declared MD5. This makes each acceptable regional version a distinct, searchable row. The `# STAGING PATHS` header comment notes that actual CSV row count may exceed unique staging path count for this reason.

### 17. `reconcile_aliases()` — post-scan alias reconciliation pass

Runs on every build immediately after `populate_missing_files` and before the
collection statistics block, so all counts in the build summary reflect the
fully reconciled state.

Three cases are handled, all MD5-based:

**Case 1 — never ingested, declared MD5 already stored elsewhere.**
For each canonical that has no row in `files` and no entry in
`canonical_aliases`, the pass checks whether any declared MD5 for that
canonical is stored in `files` under a different `canonical_name`. If found,
a row is inserted into `canonical_aliases` and any `missing_files` rows for
that canonical are deleted. No blob is written or removed.

**Case 2 — `mismatch_accepted` blob superseded by a verified alias.**
For each `mismatch_accepted` blob in `files`, the pass checks whether any
declared MD5 for that canonical is stored as `verified` under a different
`canonical_name`. If found, the stale mismatch blob is removed via
`remove_sqlar_entry()` (which also cleans `canonical_aliases`, `file_platforms`,
and `accepted_hashes`), and the canonical is registered as an alias of the
verified primary.

**Case 3 — `unverifiable` blob superseded by a verified alias.**
For each `unverifiable` blob in `files`, the pass checks whether the blob's
actual stored MD5 matches a `verified` blob under a different `canonical_name`.
This handles the common case where `find_in_manifest()` matched a file by
filename to an unverifiable canonical before the hash-based lookup could match
it to the correct verified canonical. If found, the stale unverifiable blob is
removed and the canonical is registered as an alias.

All three cases print a verbose line per resolution and emit a summary count
(`N alias(es) registered, N stale blob(s) removed`) at the end. If nothing
requires resolution the pass reports cleanly with zero counts.

Note: the unverifiable case (Case 3) uses the blob's actual stored MD5, not
any declared MD5, because unverifiable canonicals have no declared hashes by
definition. A future improvement (see ROADMAP item 1) will add stronger
signals — size matching, CRC32, cross-platform corroboration — for cases
where even the actual stored MD5 cannot be matched.

### 18. `remove_sqlar_entry()` — complete blob removal

`remove_sqlar_entry()` deletes a blob and all associated metadata in a single
call. It removes rows from five tables:

```
sqlar             WHERE name       = sqlar_name
files             WHERE sqlar_name = sqlar_name
file_platforms    WHERE sqlar_name = sqlar_name
accepted_hashes   WHERE sqlar_name = sqlar_name
canonical_aliases WHERE sqlar_name = sqlar_name
```

The `canonical_aliases` deletion is critical. Without it, any alias entry
pointing at a deleted blob becomes a dangling pointer — a `canonical_name`
recorded as resolved but with no backing blob in `sqlar`. These dangling
entries are invisible during a build run (they satisfy `_canonical_in_db()`'s
alias check, so the canonical is not flagged as missing) but fail silently
during restore: `bios_restore.py` skips alias entries whose target `sqlar_name`
does not exist, so the canonical ends up unresolved after restore.

Every caller that deletes a blob — `_cleanup_superseded()`, the
reconciliation pass Cases 2 and 3, and `audit_sqlar()` — goes through
`remove_sqlar_entry()`, so no partial deletion path exists.
