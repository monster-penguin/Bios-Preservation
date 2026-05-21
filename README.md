# BIOS Preservation Tool

A Python tool for collecting, verifying, organising, and staging retro gaming BIOS files across multiple emulation platforms. Runs on Windows, macOS, and Linux.

> **This tool does not distribute, download, or host BIOS files.** It operates exclusively on files you supply from your own sources.

---

## Documentation

Full documentation is in the [project wiki](https://github.com/monster-penguin/BIOS-Preservation-Tool/wiki):

- **[Home](https://github.com/monster-penguin/BIOS-Preservation-Tool/wiki/Home)** — overview and page index
- **[Installation](https://github.com/monster-penguin/BIOS-Preservation-Tool/wiki/Installation)** — prerequisites, setup, and first run
- **[How to Run](https://github.com/monster-penguin/BIOS-Preservation-Tool/wiki/How-to-Run)** — interactive menu and command-line usage
- **[Workflow](https://github.com/monster-penguin/BIOS-Preservation-Tool/wiki/Workflow)** — Update → Build → Report → Stage → Backup → Restore → Configure
- **[Supported Platforms](https://github.com/monster-penguin/BIOS-Preservation-Tool/wiki/Supported-Platforms)** — full platform table and notes
- **[Core Concepts](https://github.com/monster-penguin/BIOS-Preservation-Tool/wiki/Core-Concepts)** — canonical, blob, verified, unverifiable, hash mismatch
- **[Understanding the Output](https://github.com/monster-penguin/BIOS-Preservation-Tool/wiki/Understanding-the-Output)** — build counts, report counts, shopping list
- **[Configuration Reference](https://github.com/monster-penguin/BIOS-Preservation-Tool/wiki/Configuration-Reference)** — full config file reference
- **[Directory Structure](https://github.com/monster-penguin/BIOS-Preservation-Tool/wiki/Directory-Structure)** — all folders and files explained
- **[Reporting Issues](https://github.com/monster-penguin/BIOS-Preservation-Tool/wiki/Reporting-Issues)**
- **[Developer Notes](https://github.com/monster-penguin/BIOS-Preservation-Tool/wiki/Developer-Notes)** — schema, internals, implementation notes
- **[Roadmap](https://github.com/monster-penguin/BIOS-Preservation-Tool/wiki/Roadmap)** — planned features and design decisions

---

## Licences

**Code** — [MIT Licence](LICENSE.md)

**Documentation** (wiki, guides, and all written documentation in this repository) —
[Creative Commons Attribution-NonCommercial-NoDerivatives 4.0 International (CC BY-NC-ND 4.0)](DOCS_LICENSE.md)

You may share the documentation with attribution, but you may not use it commercially or republish modified versions of it without permission.

---

## Quick Start

```bash
# 1. Install dependencies
pip install pyyaml py7zr rarfile

# 2. Download platform hash metadata
python bios_preservation.py update

# 3. Scan your BIOS files
python bios_preservation.py build

# 4. Generate status reports
python bios_preservation.py report

# 5. Stage verified files into platform-ready folders
python bios_preservation.py stage
```

See the [wiki](https://github.com/monster-penguin/BIOS-Preservation-Tool/wiki) for full details.
