"""
bios_update.py — Script 1 (update)

Parses all retrobios platform YAMLs (from URL or local directory) and produces:
  - combined_platform_manifest.json
  - combined_platform_manifest.csv

Expects to live in the scripts/ subfolder of the bios_preservation root.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import urllib.request
import configparser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml is required.  pip install pyyaml")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLATFORMS = [
    "retrodeck", "retropie", "batocera", "emudeck",
    "recalbox", "retrobat", "lakka", "retroarch",
    "romm", "bizhawk",
]
YAML_FILENAMES = {p: f"{p}.yml" for p in PLATFORMS}
HASH_TYPES = ("md5", "sha1", "sha256", "crc32")


# ---------------------------------------------------------------------------
# Path helper
# ---------------------------------------------------------------------------

def _resolve(path: str, base_dir: str) -> str:
    """Return *path* as absolute, resolving relative paths against *base_dir*."""
    p = Path(path)
    if not p.is_absolute():
        p = Path(base_dir) / p
    return str(p)


# ---------------------------------------------------------------------------
# YAML loading / caching
# ---------------------------------------------------------------------------

def _fetch_url(url: str) -> str | None:
    """Download text from *url*; return None on failure."""
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.read().decode("utf-8")
    except Exception as exc:
        print(f"    WARNING: could not fetch {url}: {exc}")
        return None


def load_yaml_from_url(
    base_url: str,
    filename: str,
    cache_dir: str | None,
    refresh: bool,
) -> dict | None:
    """
    Download *filename* from *base_url*.

    If *cache_dir* is set:
      - When refresh=False and a cached copy exists, load it instead of downloading.
      - When a download succeeds, save/overwrite the cached copy.
    """
    cache_path = Path(cache_dir) / filename if cache_dir else None

    # Use cache when available and refresh not requested
    if cache_path and cache_path.exists() and not refresh:
        print(f"  Loading {filename} from cache")
        return _load_yaml_file(str(cache_path))

    # Download
    url = base_url.rstrip("/") + "/" + filename
    print(f"  Fetching {url}")
    text = _fetch_url(url)
    if text is None:
        # Fall back to stale cache if we have one
        if cache_path and cache_path.exists():
            print(f"    WARNING: download failed; using stale cache for {filename}")
            return _load_yaml_file(str(cache_path))
        return None

    # Parse first — don't cache invalid YAML
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        print(f"    WARNING: YAML parse error in {filename}: {exc}")
        return None

    # Write to cache
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            cache_path.write_text(text, encoding="utf-8")
        except OSError as exc:
            print(f"    WARNING: could not write YAML cache {cache_path}: {exc}")

    return data


def load_yaml_local(directory: str, filename: str) -> dict | None:
    path = Path(directory) / filename
    if not path.exists():
        print(f"  WARNING: {path} not found")
        return None
    return _load_yaml_file(str(path))

def load_shared_groups(
    yaml_source: str,
    yaml_url_base: str,
    yaml_local_dir: str,
    yaml_cache_dir: str | None,
    yaml_refresh: bool,
) -> dict:
    """
    Load _shared.yml and return the contents of its shared_groups key.
    Returns an empty dict if the file is missing or unparseable.
    """
    filename = "_shared.yml"
    print(f"  Loading {filename} ...")

    if yaml_source == "url":
        data = load_yaml_from_url(yaml_url_base, filename, yaml_cache_dir, yaml_refresh)
    else:
        data = load_yaml_local(yaml_local_dir, filename)

    if not data:
        print(f"  WARNING: {filename} not loaded -- includes: references will not resolve")
        return {}

    groups = data.get("shared_groups") or {}
    names = ", ".join(sorted(groups))
    print(f"  {filename}: {len(groups)} shared group(s) loaded ({names})")
    return groups


def _load_yaml_file(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except Exception as exc:
        print(f"  WARNING: could not read {path}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Inheritance resolution
# ---------------------------------------------------------------------------

def _resolve_inheritance(
    name: str, raw_map: dict[str, dict], _seen: set | None = None
) -> dict:
    """Return a fully merged YAML dict for *name*, resolving `inherits:` chains."""
    if _seen is None:
        _seen = set()
    if name in _seen:
        print(f"  WARNING: circular inheritance detected for '{name}'; stopping")
        return {}
    _seen.add(name)

    raw = raw_map.get(name) or {}
    parent_name = raw.get("inherits")
    if not parent_name:
        return dict(raw)

    parent = _resolve_inheritance(str(parent_name).strip().lower(), raw_map, _seen)

    # Deep-merge systems: parent base, child overrides per file (by name)
    parent_systems: dict = parent.get("systems") or {}
    child_systems:  dict = raw.get("systems") or {}

    merged_systems: dict = {}
    for sys_name, sys_data in parent_systems.items():
        merged_systems[sys_name] = dict(sys_data or {})

    for sys_name, sys_data in child_systems.items():
        sys_data = sys_data or {}
        if sys_name in merged_systems:
            parent_files_by_name = {
                f["name"].lower(): f
                for f in (merged_systems[sys_name].get("files") or [])
                if f and "name" in f
            }
            for cf in (sys_data.get("files") or []):
                if cf and "name" in cf:
                    parent_files_by_name[cf["name"].lower()] = cf
            ms = dict(merged_systems[sys_name])
            ms["files"] = list(parent_files_by_name.values())
            merged_systems[sys_name] = ms
        else:
            merged_systems[sys_name] = dict(sys_data)

    merged: dict = {}
    merged.update(parent)
    for key, val in raw.items():
        if key not in ("systems", "inherits"):
            merged[key] = val
    merged["systems"] = merged_systems
    return merged


def resolve_all(raw_map: dict[str, dict]) -> dict[str, dict]:
    return {name: _resolve_inheritance(name, raw_map) for name in raw_map}


# ---------------------------------------------------------------------------
# YAML processing
# ---------------------------------------------------------------------------

def _parse_hashes(entry: dict) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {ht: [] for ht in HASH_TYPES}
    for ht in HASH_TYPES:
        raw = entry.get(ht)
        if raw is not None:
            vals = [v.strip() for v in str(raw).split(",") if v.strip()]
            result[ht] = vals
    return result


def process_platform(
    platform_name: str,
    data: dict,
    global_shared_groups: dict | None = None,
) -> dict[str, dict]:
    """
    Process a resolved platform YAML.
    Returns {canonical_filename_lower: file_info_dict}.

    global_shared_groups: dict parsed from _shared.yml  →  {group_name: [file_dict, ...]}
    The platform YAML may also carry its own shared_groups (same schema); both
    are consulted when resolving includes:, with the platform-local one taking
    precedence for any group name collision.
    """
    if not data:
        return {}

    # _shared.yml schema:  shared_groups: { group_name: [file, file, ...] }
    # (a bare list per group, NOT wrapped in a "files:" key)
    global_groups: dict = global_shared_groups or {}

    # Some platform YAMLs carry inline shared_groups with the same schema
    local_groups: dict = data.get("shared_groups") or {}

    # Merge: local overrides global for the same group name
    merged_shared: dict = {**global_groups, **local_groups}

    systems: dict        = data.get("systems") or {}
    result: dict[str, dict] = {}

    for system_name, system_data in systems.items():
        system_data = system_data or {}
        files: list = list(system_data.get("files") or [])

        # Expand includes: references.
        # _shared.yml groups are bare lists; accommodate both bare-list and
        # legacy {files: [...]} wrappers just in case.
        for inc in (system_data.get("includes") or []):
            group = merged_shared.get(inc)
            if group is None:
                print(f"  WARNING: [{platform_name}] includes unknown group {inc!r}")
                continue
            if isinstance(group, list):
                # Correct schema: the group IS the file list
                files.extend(group)
            elif isinstance(group, dict):
                # Legacy / alternative schema with a "files:" wrapper
                files.extend(group.get("files") or [])

        for entry in files:
            if not entry or "name" not in entry:
                continue

            canonical   = str(entry["name"]).lower()
            destination = str(entry.get("destination") or entry["name"])
            required    = bool(entry.get("required", False))
            aliases     = [str(a) for a in (entry.get("aliases") or [])]
            hashes      = _parse_hashes(entry)
            entry_size  = entry.get("size")
            expected_size = int(entry_size) if entry_size is not None else None

            if canonical not in result:
                result[canonical] = {
                    "destinations":  [],
                    "required":      False,
                    "hashes":        {ht: [] for ht in HASH_TYPES},
                    "system":        system_name,
                    "aliases":       [],
                    "expected_size": None,
                }

            fi = result[canonical]
            if destination not in fi["destinations"]:
                fi["destinations"].append(destination)
            if required:
                fi["required"] = True
            for a in aliases:
                if a not in fi["aliases"]:
                    fi["aliases"].append(a)
            for ht in HASH_TYPES:
                existing = set(fi["hashes"][ht])
                existing.update(hashes[ht])
                fi["hashes"][ht] = sorted(existing)
            # Keep first non-None size seen (sizes should be consistent across platforms)
            if fi["expected_size"] is None and expected_size is not None:
                fi["expected_size"] = expected_size

    return result


# ---------------------------------------------------------------------------
# Manifest building
# ---------------------------------------------------------------------------

def build_manifest(
    platform_data: dict[str, dict[str, dict]],
    resolved: dict[str, dict],
) -> dict:
    all_canonicals: set[str] = set()
    for pfiles in platform_data.values():
        all_canonicals.update(pfiles.keys())

    sorted_canonicals = sorted(all_canonicals)

    platform_meta: dict[str, dict] = {}
    for p in PLATFORMS:
        pdata = resolved.get(p) or {}
        platform_meta[p] = {
            "base_destination": str(pdata.get("base_destination") or ""),
            "display_name":     str(pdata.get("platform") or p),
        }

    manifest: dict = {
        "generated_at":      datetime.now(timezone.utc).isoformat(),
        "platforms":         PLATFORMS,
        "platform_metadata": platform_meta,
        "files":             {},
    }

    for idx, canonical in enumerate(sorted_canonicals, start=1):
        db_filename = f"{idx:06d}"
        platforms_entry: dict[str, dict] = {}

        for p in PLATFORMS:
            pfiles = platform_data.get(p) or {}
            if canonical in pfiles:
                fi = pfiles[canonical]
                platforms_entry[p] = {
                    "known_file":       True,
                    "aliases":          fi.get("aliases") or [],
                    "staging_paths":    fi.get("destinations") or [],
                    "expected_hashes":  fi["hashes"],
                    "expected_size":    fi.get("expected_size"),
                    "required":         fi.get("required", False),
                }
            else:
                platforms_entry[p] = {
                    "known_file":      False,
                    "aliases":         [],
                    "staging_paths":   [],
                    "expected_hashes": {ht: [] for ht in HASH_TYPES},
                    "expected_size":   None,
                    "required":        False,
                }

        manifest["files"][canonical] = {
            "database_filename": db_filename,
            "size":   None,
            "sha1":   None,
            "md5":    None,
            "sha256": None,
            "crc32":  None,
            "platforms": platforms_entry,
        }

    return manifest


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def write_json(manifest: dict, path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
    print(f"  JSON written → {path}")


def write_csv(manifest: dict, path: str) -> None:
    headers = ["database_filename", "size", "sha1", "md5", "sha256", "crc32"]
    for p in PLATFORMS:
        headers += [
            f"{p}_known_file",
            f"{p}_aliases",
            f"{p}_staging_path",
            f"{p}_expected_hashes",
        ]

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
                pdata = fdata["platforms"].get(p)
                if not pdata or not pdata["known_file"]:
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
                        for hv in (pdata["expected_hashes"].get(ht) or []):
                            hash_parts.append(f"{ht}:{hv}")
                    row[f"{p}_expected_hashes"] = ",".join(hash_parts) if hash_parts else "unverifiable"
                writer.writerow(row)

    print(f"  CSV  written → {path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(config: configparser.ConfigParser, base_dir: str = ".") -> bool:
    section = "update"

    yaml_source    = config.get(section, "yaml_source",    fallback="url").strip().lower()
    yaml_url_base  = config.get(section, "yaml_url_base",  fallback="https://raw.githubusercontent.com/Abdess/retrobios/main/platforms/")
    yaml_local_dir = _resolve(config.get(section, "yaml_local_dir", fallback="yaml"), base_dir)
    yaml_cache_dir = _resolve(config.get(section, "yaml_cache_dir", fallback="yaml"), base_dir)
    yaml_refresh   = config.getboolean(section, "yaml_refresh", fallback=False)
    output_dir     = _resolve(config.get(section, "output_dir", fallback="update"), base_dir)

    os.makedirs(output_dir, exist_ok=True)

    # -- 1. Load _shared.yml (global shared groups) --------------------------------
    mode_label = "URL" if yaml_source == "url" else "local directory"
    print(f"[update] Loading YAMLs from {mode_label} ...")

    global_shared_groups = load_shared_groups(
        yaml_source, yaml_url_base, yaml_local_dir, yaml_cache_dir, yaml_refresh
    )

    # -- 2. Load platform YAMLs -------------------------------------------------------
    raw_map: dict[str, dict] = {}
    for pname in PLATFORMS:
        fname = YAML_FILENAMES[pname]
        if yaml_source == "url":
            data = load_yaml_from_url(yaml_url_base, fname, yaml_cache_dir, yaml_refresh)
        else:
            data = load_yaml_local(yaml_local_dir, fname)
        raw_map[pname] = data or {}

    # -- 3. Resolve inheritance -------------------------------------------------------
    print("[update] Resolving YAML inheritance ...")
    resolved = resolve_all(raw_map)

    # -- 4. Process each platform -----------------------------------------------------
    print("[update] Processing platform data ...")
    platform_data: dict[str, dict] = {}
    for pname in PLATFORMS:
        platform_data[pname] = process_platform(pname, resolved[pname], global_shared_groups)
        print(f"  {pname:12s}: {len(platform_data[pname])} files")

    # ── 4. Build manifest ──────────────────────────────────────────────────
    print("[update] Building combined manifest …")
    manifest = build_manifest(platform_data, resolved)
    total = len(manifest["files"])
    print(f"  Total unique canonical files: {total}")

    # ── 5. Write outputs ───────────────────────────────────────────────────
    json_path = os.path.join(output_dir, "combined_platform_manifest.json")
    csv_path  = os.path.join(output_dir, "combined_platform_manifest.csv")
    write_json(manifest, json_path)
    write_csv(manifest, csv_path)

    print(f"[update] Done.  {total} files catalogued.")
    return True


# ---------------------------------------------------------------------------
# Stand-alone execution
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # When run directly from scripts/, find conf in ../configure/
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
