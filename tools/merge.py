#!/usr/bin/env python3
"""Merge source mods into this mod.

Reads merge-config.json and copies content from each listed mod in order.
Later mods overwrite files from earlier mods on conflict. This mod's own
content lives in custom/ and is copied last, over everything.

Source mods are named by folder rather than by absolute path, and each one is
looked up in the local mod folder, the local mod-storage folder, and the Steam
Workshop folder for EU5, in that order.

Usage:
    python tools/merge.py
    python tools/merge.py --check    Resolve every mod and stop, copying nothing
"""

import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
MOD_ROOT = SCRIPT_DIR.parent
CONFIG_PATH = MOD_ROOT / "merge-config.json"
CUSTOM_DIR = MOD_ROOT / "custom"

# EU5 mod content directories - everything else is metadata/tooling
CONTENT_DIRS = {"in_game", "main_menu", "loading_screen"}

EU5_DOCS_SUBPATH = Path("Paradox Interactive") / "Europa Universalis V"
WORKSHOP_APP_ID = "3450310"


def _force_remove(func, path, exc):
    """Handle permission errors during rmtree (common with OneDrive sync)."""
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    func(path)


def _registry_value(hive, key, name):
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(hive, key) as handle:
            value, _ = winreg.QueryValueEx(handle, name)
            return str(value)
    except OSError:
        return None


def find_eu5_documents_dir():
    """Locate the Paradox Interactive/Europa Universalis V user directory."""
    # This repo normally sits in that directory's mod folder.
    for parent in MOD_ROOT.parents:
        if parent.name == "Europa Universalis V" and parent.parent.name == "Paradox Interactive":
            return parent

    candidates = []

    # The Documents folder as Windows records it, which follows OneDrive redirection.
    try:
        import winreg

        documents = _registry_value(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            "Personal",
        )
        if documents:
            candidates.append(Path(os.path.expandvars(documents)))
    except ImportError:
        pass

    for variable in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial", "USERPROFILE", "HOME"):
        base = os.environ.get(variable)
        if base:
            candidates.append(Path(base) / "Documents")

    for candidate in candidates:
        eu5_dir = candidate / EU5_DOCS_SUBPATH
        if eu5_dir.is_dir():
            return eu5_dir

    return None


def find_steam_libraries():
    """Return every Steam library folder on this machine."""
    libraries = []
    seen = set()

    def add(path):
        if not path:
            return
        resolved = Path(str(path))
        key = str(resolved).lower()
        if key not in seen and resolved.is_dir():
            seen.add(key)
            libraries.append(resolved)

    try:
        import winreg

        add(_registry_value(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"))
        add(_registry_value(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"))
        add(_registry_value(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"))
    except ImportError:
        pass

    for fallback in ("C:/Steam", "C:/Program Files (x86)/Steam", "C:/Program Files/Steam"):
        add(fallback)
    home = os.environ.get("HOME")
    if home:
        add(Path(home) / ".steam" / "steam")
        add(Path(home) / ".local" / "share" / "Steam")

    # Additional library folders are listed in the main install's libraryfolders.vdf.
    for library in list(libraries):
        for vdf in (library / "steamapps" / "libraryfolders.vdf", library / "config" / "libraryfolders.vdf"):
            if vdf.is_file():
                text = vdf.read_text(encoding="utf-8", errors="replace")
                for match in re.finditer(r'"path"\s*"([^"]+)"', text):
                    add(match.group(1).replace("\\\\", "\\"))

    return libraries


def find_search_roots():
    """Return the folders searched for source mods, in priority order."""
    roots = []
    eu5_dir = find_eu5_documents_dir()
    if eu5_dir:
        for name in ("mod", "mod-storage"):
            candidate = eu5_dir / name
            if candidate.is_dir():
                roots.append((name + "/", candidate))
    for library in find_steam_libraries():
        candidate = library / "steamapps" / "workshop" / "content" / WORKSHOP_APP_ID
        if candidate.is_dir():
            roots.append(("workshop/" + WORKSHOP_APP_ID + "/", candidate))
    return roots


def resolve_mod(mod, roots):
    """Find a configured mod's folder. Returns (path, origin) or (None, None)."""
    folder = mod.get("folder")
    names = [folder] if isinstance(folder, str) else list(folder or [])
    for name in names:
        head, _, tail = name.replace("\\", "/").partition("/")
        for origin, root in roots:
            candidate = root / head
            if tail:
                candidate = candidate / tail
            if candidate.is_dir():
                return candidate, origin + name
    return None, None


def copy_content(source):
    """Copy a source's content directories over this mod's. Returns a summary."""
    copied = []
    for item in sorted(source.iterdir()):
        if item.name in CONTENT_DIRS and item.is_dir():
            shutil.copytree(item, MOD_ROOT / item.name, dirs_exist_ok=True)
            file_count = sum(1 for f in item.rglob("*") if f.is_file())
            copied.append(f"{item.name}/ ({file_count})")
    return ", ".join(copied)


def folder_names(mod):
    folder = mod.get("folder")
    return folder if isinstance(folder, str) else ", ".join(folder or [])


def main():
    check_only = "--check" in sys.argv[1:]

    if not CONFIG_PATH.exists():
        print(f"ERROR: Config not found: {CONFIG_PATH}")
        sys.exit(1)

    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)

    mods = config.get("mods", [])
    if not mods:
        print("No mods configured in merge-config.json.")
        return

    roots = find_search_roots()
    if not roots:
        print("ERROR: Found no mod, mod-storage, or Steam Workshop folder to search.")
        print("Expected a Paradox Interactive/Europa Universalis V folder above this mod.")
        sys.exit(1)

    print("Searching:")
    for _, root in roots:
        print(f"  {root}")
    print()

    resolved = []
    missing = []
    for mod in mods:
        path, origin = resolve_mod(mod, roots)
        if path:
            resolved.append((mod, path, origin))
        else:
            missing.append(f"  {mod.get('name', '???')}: {folder_names(mod)}")
    if missing:
        print("ERROR: Missing mod folders:")
        print("\n".join(missing))
        print("\nEnsure these mods are subscribed in Steam or cloned locally.")
        sys.exit(1)

    if check_only:
        print(f"Resolved {len(resolved)} mod(s):\n")
        for i, (mod, _, origin) in enumerate(resolved, 1):
            print(f"  {i}. {mod.get('name', origin)}: {origin}")
        return

    print(f"Merging {len(resolved)} mod(s)...\n")

    # Clean content directories
    for dir_name in CONTENT_DIRS:
        target = MOD_ROOT / dir_name
        if target.exists():
            shutil.rmtree(target, onexc=_force_remove)

    # Merge each mod in order (first = lowest priority, last = highest)
    for i, (mod, mod_path, origin) in enumerate(resolved, 1):
        merged = copy_content(mod_path)
        mod_name = mod.get("name", origin)
        print(f"  {i}. {mod_name}: {merged or '(no content directories)'}")
        print(f"     {origin}")
        link = mod.get("link", "")
        if link:
            print(f"     {link}")

    # This mod's own content, applied over every source mod
    if CUSTOM_DIR.is_dir():
        merged = copy_content(CUSTOM_DIR)
        print(f"  {len(resolved) + 1}. This mod's own content: {merged or '(empty)'}")
        print("     custom/")

    print("\nDone!")


if __name__ == "__main__":
    main()
