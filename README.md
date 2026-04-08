# BIOS Preservation Tool

A Python tool for collecting, verifying, organising, and staging retro gaming BIOS files across multiple emulation platforms. Runs on Windows, macOS, and Linux.

---

## Table of Contents

1. [What This Tool Does](#what-this-tool-does)
2. [Legal & Copyright Notice](#legal--copyright-notice)
3. [Upstream Dependency: Abdess/retrobios](#upstream-dependency-abdessretrobios)
4. [Prerequisites](#prerequisites)
5. [Installation](#installation)
6. [Quick Start](#quick-start)
7. [How to Run](#how-to-run)
8. [Supported Platforms](#supported-platforms)
9. [Core Concepts](#core-concepts)
10. [Workflow](#workflow)
11. [Understanding the Output](#understanding-the-output)
12. [Configuration Reference](#configuration-reference)
13. [Directory Structure](#directory-structure)
14. [Reporting Issues](#reporting-issues)

---

## What This Tool Does

This tool does **not** distribute, download, or host BIOS files. Given a collection of BIOS files you already own, it:

- **Verifies** them against cryptographic hashes declared by major retro gaming platforms
- **Stores** them in a deduplicated, hash-addressed local database
- **Stages** correctly named copies into ready-to-use directories for any supported platform
- **Reports** on the status of your collection — what you have, what's missing, what's the wrong version
- **Generates a shopping list** of files still needed, with expected MD5 hashes to search for

The typical workflow: point the tool at your BIOS collection → Build to ingest and verify → Report to see where you stand → Stage to produce a platform-ready BIOS folder.

---

## Legal & Copyright Notice

**This tool does not host, download, or distribute BIOS files.** It operates exclusively on files you supply from your own sources.

BIOS files are copyrighted firmware owned by their respective hardware manufacturers. Distributing them without authorisation is illegal in most jurisdictions. This tool is designed to be insulated from that risk:

- No BIOS data ever passes through any server operated by this project
- The tool downloads only hash metadata (MD5, SHA1, CRC32 values and filenames) from the upstream Abdess/retrobios project — no actual file content
- All BIOS storage and staging happens entirely on your local machine
- The database (`bios_database.sqlar`) lives on your disk and is never transmitted anywhere

It is your responsibility to ensure that any BIOS files you provide are ones you are legally entitled to possess.

---

## Upstream Dependency: Abdess/retrobios

This tool relies on the [Abdess/retrobios](https://github.com/Abdess/retrobios) project for hash metadata. That project maintains YAML files for each supported platform, declaring expected filenames, cryptographic hashes (MD5, SHA1, SHA256, CRC32), file sizes, and staging paths for thousands of BIOS files — sourced directly from emulator source code.

### What the dependency means in practice

The Update step downloads these YAML files from GitHub and caches them locally in `yaml_cache/`. Cached copies are reused on subsequent runs unless you set `yaml_refresh = true`. If the upstream project is unavailable, the tool continues working from its cache.

To run entirely offline, set `yaml_source = local` in your config and point `yaml_local_dir` at your cached YAML files.

### What happens if the upstream project disappears

The YAML cache is a complete offline copy of everything the tool needs. A collection built against a cached manifest continues to work indefinitely — the manifest is frozen at the time of the last Update run. You would only need a fresh Update to pick up newly supported systems or corrected hash values from upstream.

The upstream project contains no BIOS file content — only metadata — so it carries no copyright hosting risk.

---

## Prerequisites

- **Python 3.10 or later** — [python.org/downloads](https://www.python.org/downloads/)
- **pip** — included with Python
- **Required Python packages:**

```bash
pip install pyyaml py7zr rarfile
```

- **UnRAR binary** (for `.rar` archive support — optional but recommended):

| OS | Command |
|----|---------|
| Windows | `winget install RARLab.WinRAR` |
| macOS | `brew install unrar` |
| Debian/Ubuntu | `sudo apt install unrar` |
| Fedora/RHEL | `sudo dnf install unrar` |

If UnRAR is not found at startup, the tool prompts you for the path and saves it to your user config for future runs. RAR support is only needed if your BIOS sources include `.rar` archives.

---

## Installation

1. Download or clone this repository
2. Install Python dependencies:

```bash
pip install pyyaml py7zr rarfile
```

3. On Windows, optionally install WinRAR for `.rar` support:

```powershell
winget install RARLab.WinRAR
```

No build step required — the tool runs directly from Python.

---

## Quick Start

```bash
# 1. Download platform hash metadata (internet required; cached for future runs)
python bios_preservation.py update

# 2. Scan your BIOS files — add source path(s) at the prompt, then press C
python bios_preservation.py build

# 3. Generate status reports for all platforms
python bios_preservation.py report

# 4. Stage verified files into platform-ready folders
python bios_preservation.py stage
```

Or chain all four steps:

```bash
python bios_preservation.py update build report stage
```

After Build, review the collection summary in the terminal. After Report, open `report/global_shopping_list.csv` to see which files are still missing or the wrong version.

---

## How to Run

### Interactive menu (recommended)

```bash
python bios_preservation.py
```

Presents a numbered menu. After each step completes, the menu returns. Enter `0` to exit.

```
============================================================
  BIOS Preservation Tool
============================================================
  1. Update
  2. Build
  3. Stage
  4. Report
  5. Backup
  6. Restore
  7. Configure
  0. Exit
────────────────────────────────────────────────────────────
  Enter number:
```

### Command-line (for scripting and automation)

```bash
python bios_preservation.py update
python bios_preservation.py build
python bios_preservation.py stage
python bios_preservation.py report
python bios_preservation.py update build stage report
python bios_preservation.py update build stage report --continue-on-error
```

Steps can be run individually or chained in any order. The launcher stops on first failure unless `--continue-on-error` is passed. A pass/fail summary is printed at the end of every run.

### File-open detection

Before any step runs, the launcher checks whether its output files are locked by another process (e.g. a CSV open in Excel, or the database open in DB Browser). If a lock is detected, the step is aborted and the locked file is named.

| Step      | Files checked |
|-----------|---------------|
| Update    | `update/combined_platform_manifest.json`, `.csv` |
| Build     | `build/bios_database.sqlar`, `combined_platform_build.json`, `.csv` |
| Stage     | `build/bios_database.sqlar`, `combined_platform_build.json` |
| Report    | `build/bios_database.sqlar`, `combined_platform_build.json`, all enabled `report/<platform>_report.csv` |
| Backup    | `build/bios_database.sqlar` |
| Dump      | `build/bios_database.sqlar` |
| Configure | _(no check)_ |

---

## Supported Platforms

| Key       | Display Name | Notes |
|-----------|-------------|-------|
| retroarch | RetroArch   | |
| batocera  | Batocera    | |
| recalbox  | Recalbox    | |
| retrobat  | RetroBat    | |
| emudeck   | EmuDeck     | |
| lakka     | Lakka       | Inherits RetroArch file list |
| retrodeck | RetroDECK   | |
| retropie  | RetroPie    | Inherits RetroArch file list; archived upstream |
| romm      | RomM        | EmulatorJS-based; stages to `bios/{platform_slug}/` |
| bizhawk   | BizHawk     | Standalone; stages to `Firmware/`; SHA1-primary |

RetroArch, Lakka, and RetroPie share the same underlying file list — identical counts across these three in report output is correct and expected.

BizHawk declares SHA1 hashes for most files rather than MD5. Files with no declared MD5 will show as `unverifiable` in the shopping list (no MD5 to search for), even when their SHA1 is known.

---

## Core Concepts

**Canonical** — a unique BIOS file identity across all platforms. Two platforms calling the same bytes by different names equals one canonical. The tool deduplicates by content, not by name.

**Blob** — one physical binary stored in the database. A canonical may have multiple blobs when different platforms accept different verified regional variants (e.g. a Japanese and a US version of the same BIOS, each with a distinct MD5). Only `verified` blobs coexist this way; `unverifiable` and `mismatch_accepted` blobs are limited to one per canonical.

**Present** — at least one blob is stored for this canonical.

**Missing** — no blob found in any scanned source yet.

**Verified** — a stored blob's hash matches a hash declared for this canonical by at least one platform.

**Unverifiable** — stored, but no platform declares any hash to check against. The file cannot be confirmed correct, but it is not missing.

**Hash mismatch** — stored, but the blob's hash matches none of the declared values. You have a file, but it appears to be the wrong version.

> **A file's identity is its hash, not its name.** `BIOS.ROM`, `bios.rom`, and `Boot.ROM` are the same file if they share the same MD5. Filename case is always preserved exactly as declared in the source YAML.

---

## Workflow

### Step 1 — Update

Downloads platform YAML files from Abdess/retrobios and produces a combined manifest — a single JSON/CSV describing every BIOS file expected by every supported platform, with declared hashes, sizes, and staging paths.

Run Update when you want to pick up newly supported systems or corrected hashes from upstream. It does not modify your database.

**Outputs** (written to `update/`):
- `combined_platform_manifest.json`
- `combined_platform_manifest.csv`

---

### Step 2 — Build

Scans your source locations, verifies files against the manifest, and stores them in the database. This is the main ingestion step.

**Configuring sources:** At the start of each Build run, an interactive screen manages scan sources. Sources are saved to your user config automatically.

```
============================================================
  BUILD — Source Configuration
============================================================
  Current sources:
    1. C:/Users/you/bios_collection
    2. C:/Users/you/Downloads/bios_pack.zip
  [A] Add a source (path, URL, or archive)
  [E] Edit / remove a source
  [C] Continue with current sources
────────────────────────────────────────────────────────────
  Enter choice [A/E/C]:
```

Sources may be local directories, local archive files (zip, 7z, rar, tar, tar.gz, tar.bz2, tar.xz), or HTTP/HTTPS URLs. Directories are scanned recursively up to 6 levels deep. Archives are opened and scanned recursively up to 6 nesting levels.

**Dual-pass scanning:** Every directory source runs two passes automatically:

- **Pass 1 — filename + hash matching:** Matches by declared filename first, then by any declared hash. Covers well-organised collections where filenames are correct.
- **Pass 2 — MD5-only matching:** Hashes every file and looks it up purely by MD5, ignoring filenames. Catches renamed or arbitrarily named files.

**Multi-variant storage:** When a canonical has multiple valid MD5 hashes declared across platforms (regional versions of the same BIOS), all distinct verified variants are stored as separate blobs. When staging, the variant whose MD5 matches what the target platform declares is used; if no match exists, the first verified variant is used.

**Build summary example:**

```
[build] Definitions:
  canonical  — one unique BIOS file identity across all platforms
  blob       — one physical binary stored in the database
  present    — at least one blob stored for this canonical
  missing    — not found in any scanned source yet
  verified   — blob hash matches a declared value
  unverifiable — stored but no declared hash to check against
  hash mismatch — stored but blob hash matches none of the declared values

[build] Collection summary — 2198 canonical(s) across all platforms:
  Present  :   2112  (at least one blob stored)
    verified          :   1740
    unverifiable      :    141
    hash mismatch     :    231
  Via alias:     54  (bytes stored under a different canonical name)
  Missing  :      0  (not yet found in any source, across all platforms)

  Blobs stored : 2816 total  (2084 verified, 129 canonical(s) with multiple verified variants)

  Shopping list: roughly 372 rows expected (0 missing + 231 mismatch + 141 unverifiable).
  Actual row count varies:
    mismatch    — expands when multiple MD5 variants are declared (one row per version).
    missing     — may consolidate when multiple canonicals share an expected MD5.
    unverifiable — may expand: alias canonicals whose primary blob is unverifiable
                  each appear as their own row alongside the primary canonical.
```

The `Missing` count is global — canonicals absent from the database across all platforms combined. Per-platform counts in the Report step cover only the files that platform declares, so they will always be lower than the totals shown above.

The `Via alias` line appears when canonicals are present whose bytes are stored under a different canonical name (see [Core Concepts](#core-concepts)). These are fully resolved and do not appear on the shopping list.

**Outputs** (written to `build/`):
- `bios_database.sqlar` — the database
- `combined_platform_build.json` — manifest with actual stored filenames filled in
- `combined_platform_build.csv` — flat CSV version of the build manifest

---

### Step 3 — Stage

Reads the database and writes a correctly named, platform-ready BIOS directory (or zip) for each selected platform.

At the start of each Stage run, a toggleable checklist lets you choose which platforms to stage. Selections apply to that run only and are not saved.

When a canonical has multiple verified variants stored, Stage selects the variant whose MD5 matches what the target platform declares. If the platform declares no MD5, or none of the declared MD5s are stored, the first verified variant is used.

**Outputs** (written to `stage/`):
- `stage/<platform>/` — one subfolder per platform (directory mode)
- `stage/<platform>_bios.zip` — one zip per platform (zip mode)

---

### Step 4 — Report

Generates one CSV status report per enabled platform and a global shopping list.

At the start of each Report run, a toggleable checklist selects which platforms to report on.

**Per-platform reports** (`report/<platform>_report.csv`):

Each CSV has three summary lines at the top:

```
# PHYSICAL FILES (matches build): total=437  present=437  verified=418  unverifiable=17  hash_mismatch=2  missing=0
# MANIFEST ENTRIES:               total=437  present=437  missing=0  hash_mismatch=5
# STAGING PATHS:                  total=441  present=441  missing=0  (counts reflect unique staging paths; ...)
```

Always use the **PHYSICAL FILES** line as the authoritative measure of your collection for that platform. Manifest entry and staging path counts are informational only — their higher numbers are a consequence of one physical file satisfying many manifest entries.

The body of the report has one row per staging path the platform expects. When a file has multiple declared MD5 variants and none match what you have stored, the report emits one row per declared MD5 so each acceptable version appears as a distinct target.

Report columns:

| Column | Description |
|--------|-------------|
| `filename` | Filename portion of the staging path (case-preserved) |
| `present` | `yes` / `no` |
| `staging_path` | Full relative staging path (e.g. `np2kai/bios.rom`) |
| `actual size` | Size in bytes from the database, blank if not present |
| `expected size` | `not present` / `match` / declared value if different |
| `actual sha1` | From the database, blank if not present |
| `expected sha1` | `not present` / `match` / declared value(s) if mismatch |
| `actual md5` | From the database, blank if not present |
| `expected md5` | `not present` / `match` / single declared MD5 (one row per variant when mismatched) |
| `actual sha256` | From the database, blank if not present |
| `expected sha256` | `not present` / `match` / declared value(s) if mismatch |
| `actual crc32` | From the database, blank if not present |
| `expected crc32` | `not present` / `match` / declared value(s) if mismatch |

**Global shopping list** (`report/global_shopping_list.csv`):

One row per declared MD5 value across all platforms combined, for files that need attention. Three filtered subsets are also written to the same folder:

- `shopping_missing.csv` — files not found in any source
- `shopping_hash_mismatch.csv` — files present but hash doesn't match any declared value
- `shopping_unverifiable.csv` — files present but no declared hash to verify against

All four files share identical columns; the subsets are pre-filtered views of the global list.

Each row represents a distinct declared MD5 that still needs attention:

| Status | Meaning |
|--------|---------|
| `missing` | No blob found in any source |
| `hash_mismatch` | A blob is stored, but its hash doesn't match any declared value |
| `unverifiable` | A blob is stored, but no platform declares a hash to verify against |

Columns:
- **Known Aliases** — all filenames this file is known by
- **Expected MD5** — the MD5 to search for (`unknown` if no MD5 is declared anywhere)
- **Status** — `missing` / `hash_mismatch` / `unverifiable`
- **Platforms** — which platforms need this file or version
- **Actual MD5** — what is currently stored (`not present` for missing files)

For `hash_mismatch` entries, **Expected MD5** is what you need to find; **Actual MD5** is the wrong version you currently have.

Console summary:
```
Global shopping list → ...\global_shopping_list.csv  (2 missing, 278 hash_mismatch, 141 unverifiable)
```

---

### Step 5 — Backup

Exports every blob stored in the database into a dated zip archive. Files are written into three status-named subfolders:

| Subfolder | Contents | Naming |
|-----------|----------|--------|
| `verified/` | Hash-confirmed files | `{md5}.{ext}` |
| `unverifiable/` | Files with no declared hashes | Full staging path, e.g. `np2kai/bios.rom` |
| `mismatch_accepted/` | Files whose hash didn't match | Full staging path |

Using full staging paths (rather than bare filenames) for `unverifiable/` and `mismatch_accepted/` prevents collisions between files that share a basename across different systems (e.g. `np2kai/bios.rom` vs `keropi/bios.rom`).

The backup zip also contains two sidecars:

- `.blob_map.json` — maps every blob's storage key to its canonical name and status. Used by Restore to identify blobs without relying on the current manifest, so restoration is robust across manifest updates.
- `.aliases.json` — records every entry in the `canonical_aliases` table. Used by Restore to re-establish alias relationships for canonicals that have no declared MD5 of their own.

Use the Restore step to load a backup zip into a fresh database.

**Output naming:** `backup/20_mar_2026_backup.zip`, `backup/20_mar_2026_backup(2).zip`, …

---

### Step 6 — Restore

Restores the database from a backup zip produced by the Backup step. Presents a numbered list of available backups in the `backup/` folder, or accepts a custom path.

For each blob in the backup:

- **Canonical still in current manifest** — stored normally into the database
- **Canonical no longer in manifest** — written to `orphans/` as `{md5}.{ext}` alongside `_orphans.json`, which maps each file to its original canonical name

Alias relationships are restored from `.aliases.json`. The Restore step always starts with a fresh database (existing database is deleted after confirmation).

If orphans are produced, run Update then add the `orphans/` folder as a Build source. Files will be re-ingested if the updated manifest declares their MD5.

**Output:** `build/bios_database.sqlar` — the restored database, ready to use.

**Orphans (if any):** `orphans/` — blobs whose canonical is no longer in the manifest, plus `_orphans.json` index.

---

### Step 7 — Configure

Interactive configuration editor. Walks through every setting in `bios_preservation.conf` and writes your choices to `configure/bios_preservation_user.conf`. User config values take precedence over defaults.

**First prompt:**
```
  1. Edit user configuration
  2. Restore defaults  (deletes user.conf)
```

- **Option 1** — walks through all sections and keys, showing current and default values. Confirm with Enter or Y, or type a new value. A summary of all changes is shown before writing.
- **Option 2** — deletes `bios_preservation_user.conf`, restoring all defaults immediately.

Changes take effect for the next menu selection without restarting.

**Not managed through Configure:**
- Source paths — managed at the start of each Build run
- Platform toggles — managed interactively at the start of each Stage and Report run
- UnRAR path — saved automatically when entered at the dependency check prompt

---

## Understanding the Output

### Build counts vs report counts

The build summary counts **canonicals across all platforms combined**. Each platform report counts only the canonicals that specific platform declares. These numbers will always differ — a canonical declared only by RetroDECK does not appear in the Batocera count.

The blob count on the `Blobs stored:` line can exceed the canonical count. This happens when multiple verified regional variants of the same BIOS are stored (e.g. Japanese and US versions of the same file). Both count as one canonical but two blobs.

The `via alias` line in the summary counts canonicals whose physical bytes are already stored under a different canonical name — they have no blob of their own but are fully resolved. They are included in the total so the summary canonical count matches the manifest.

### Shopping list row count

The shopping list row count shown at the end of Report will often differ from the estimate printed by Build. This is expected:

- **Mismatch expands** — a canonical with 3 declared MD5 variants that all fail produces 3 rows (one per version to hunt for)
- **Missing may consolidate** — multiple canonicals sharing the same expected MD5 merge into one row
- **Unverifiable may expand** — alias canonicals whose primary blob is unverifiable each appear as their own row alongside the primary canonical, so the row count can exceed the unverifiable canonical count

### Status is never downgraded

A `verified` blob can never be displaced by a `mismatch_accepted` or `unverifiable` blob, regardless of scan order. When a better copy is found, the lower-status blob is deleted and replaced. When multiple verified variants of the same canonical exist, all are kept.

For `unverifiable` and `mismatch_accepted` blobs, only one is kept per canonical at a time. If the same canonical is encountered again from a different source at the same status, the duplicate is silently discarded. A status upgrade — e.g. a subsequent scan finds an `unverifiable` copy for a canonical previously stored as `mismatch_accepted` — replaces the existing blob.

---

## Configuration Reference

### Default config

`configure/bios_preservation.conf` — shipped with the tool. Do not edit this file directly; use Configure (Step 7) or edit `bios_preservation_user.conf` manually.

### User config

`configure/bios_preservation_user.conf` — created and managed by Configure. Loaded on top of the default config; user values take precedence. Delete this file to restore all defaults.

The master launcher announces which config is active at startup:
```
[launcher] Using user configuration: .../configure/bios_preservation_user.conf
```

### Full config reference

```ini
[update]
yaml_source    = url         # "url" to download + cache, or "local" for yaml_local_dir only
yaml_url_base  = https://raw.githubusercontent.com/Abdess/retrobios/main/platforms/
yaml_local_dir = yaml_cache  # local YAML directory (also used as download cache)
yaml_cache_dir = yaml_cache
yaml_refresh   = false       # true = always re-download, ignoring cache
output_dir     = update

[build]
manifest_input = update/combined_platform_manifest.json
sqlar_output   = build/bios_database.sqlar
json_output    = build/combined_platform_build.json
csv_output     = build/combined_platform_build.csv
incremental    = true        # false = delete database and rebuild from scratch
temp_dir       = temp        # .7z extraction workspace; point at a larger drive if needed
# Source locations — also managed interactively at runtime
# source_1 = C:/Users/you/bios_collection
# source_2 = C:/Users/you/Downloads/bios_pack.zip
# source_3 = https://example.com/bios_archive.zip

[stage]
sqlar_input    = build/bios_database.sqlar
manifest_input = build/combined_platform_build.json
stage_dir      = stage
output_format  = directory   # or "zip"
# Per-platform defaults for the interactive checklist
retrodeck = yes
retropie  = yes
batocera  = yes
emudeck   = no
recalbox  = yes
retrobat  = no
lakka     = yes
retroarch = yes
romm      = no
bizhawk   = no

[report]
sqlar_input    = build/bios_database.sqlar
manifest_input = build/combined_platform_build.json
report_dir     = report
# Per-platform defaults for the interactive checklist
retrodeck = yes
retropie  = yes
batocera  = yes
emudeck   = yes
recalbox  = yes
retrobat  = yes
lakka     = yes
retroarch = yes
romm      = yes
bizhawk   = yes

[backup]
backup_dir = backup
```

---

## Directory Structure

```
bios_preservation/
├── bios_preservation.py              ← master launcher (entry point)
├── configure/
│   ├── bios_preservation.conf        ← default config (do not edit directly)
│   └── bios_preservation_user.conf   ← your overrides (auto-created by Configure)
├── scripts/
│   ├── bios_update.py                ← Step 1: parse YAMLs, build manifest
│   ├── bios_build.py                 ← Step 2: scan sources, build database
│   ├── bios_stage.py                 ← Step 3: stage files per platform
│   ├── bios_report.py                ← Step 4: generate per-platform reports
│   ├── bios_backup.py                ← Step 5: export all blobs to a dated zip
│   ├── bios_restore.py               ← Step 6: restore database from a backup zip
│   └── bios_configure.py             ← Step 7: interactive configuration editor
├── yaml_cache/                       ← downloaded platform YAML files (auto-created)
├── update/                           ← manifest outputs (auto-created)
├── build/                            ← database and build manifests (auto-created)
├── stage/                            ← staged platform output (auto-created)
├── report/                           ← per-platform CSV reports (auto-created)
├── backup/                           ← dated backup zips (auto-created)
├── orphans/                          ← blobs orphaned during restore (auto-created if needed)
└── temp/                             ← .7z extraction workspace (auto-created)
```

All relative paths in `bios_preservation.conf` resolve from the `bios_preservation/` root. The tool can be run from any working directory.

---

## Reporting Issues

If you encounter a bug or unexpected behaviour:

1. Note the exact step that failed and the terminal output around the error
2. Note your OS, Python version (`python --version`), and package versions (`pip show pyyaml py7zr rarfile`)
3. Open an issue with that information

For issues with incorrect hash values, wrong staging paths, or missing platform support, the upstream source is [Abdess/retrobios](https://github.com/Abdess/retrobios) — those values come from the platform YAML files maintained there, not from this tool.
