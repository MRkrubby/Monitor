#!/usr/bin/env python3
"""Build the distributable QGIS Monitor Pro ZIP package."""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DIST_DIR = ROOT / "dist"
ZIP_NAME = DIST_DIR / "qgis_monitor_pro_3_3_13_logsafe.zip"
PLUGIN_DIRNAME = "qgis_monitor_pro"
PACKAGE_README = ROOT / "PACKAGE_README.md"

# Files that should not end up inside the plugin directory when packaging.
EXCLUDE = {
    "README.md",  # repository level instructions
    ZIP_NAME.name,
    PACKAGE_README.name,
    "scripts/build_package.py",
}

# Always include these files even if they do not match the suffix filter.
FORCED = {
    "metadata.txt",
    "CHANGELOG.md",
}

ALLOWED_SUFFIXES = {
    ".py",
    ".png",
    ".qss",
    ".txt",
    ".md",
}


EXCLUDE_DIRS = {".git", "dist", "__pycache__"}


def _from_git() -> list[Path]:
    git_files = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True)
    selected: list[Path] = []
    for line in git_files.splitlines():
        if not line or line in EXCLUDE:
            continue
        path = Path(line)
        if path.name in FORCED or path.suffix in ALLOWED_SUFFIXES:
            selected.append(path)
    return selected


def _from_filesystem() -> list[Path]:
    selected: list[Path] = []
    for path in ROOT.rglob("*"):
        if path.is_dir():
            continue
        rel = path.relative_to(ROOT)
        if rel.as_posix() in EXCLUDE:
            continue
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if rel.name in FORCED or rel.suffix in ALLOWED_SUFFIXES:
            selected.append(rel)
    return selected


def gather_files() -> list[Path]:
    """Return the files that should be part of the plugin package."""
    try:
        return _from_git()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return _from_filesystem()


def build(zip_path: Path) -> None:
    files = gather_files()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        if PACKAGE_README.exists():
            zf.write(PACKAGE_README, "README.md")
        for path in files:
            zf.write(ROOT / path, f"{PLUGIN_DIRNAME}/{path.as_posix()}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ZIP_NAME,
        help="Path to the ZIP archive to create",
    )
    args = parser.parse_args()
    output_path = args.output
    if not output_path.is_absolute():
        output_path = ROOT / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    build(output_path)
    try:
        relative = output_path.relative_to(ROOT)
    except ValueError:
        relative = output_path
    print(f"Wrote {relative}")


if __name__ == "__main__":
    main()
