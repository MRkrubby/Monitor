#!/usr/bin/env python3
"""Build and sanity-check the plugin before committing to ``main``.

This helper combines the packaging script with a bytecode compilation
smoke test so maintainers have a single command to run before uploading
changes.  It leaves committing and pushing to the caller but prints a
concise checklist with the relevant git commands.
"""
import argparse
import subprocess
import sys
from pathlib import Path


def run(command, cwd):
    """Execute *command* inside *cwd* while echoing the invocation."""
    print(f"$ {' '.join(command)}")
    subprocess.check_call(command, cwd=cwd)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Package the plugin and run quick checks before publishing."
    )
    parser.add_argument(
        "--skip-package",
        action="store_true",
        help="Skip regenerating the dist/ ZIP archive.",
    )
    parser.add_argument(
        "--skip-compile",
        action="store_true",
        help="Skip the Python bytecode compilation smoke test.",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]

    if not args.skip_package:
        run([sys.executable, "scripts/build_package.py"], cwd=repo_root)

    if not args.skip_compile:
        compile_targets = [
            "plugin.py",
            "qgis_monitor.py",
            "settings_ui.py",
            "log_viewer.py",
            "scripts",
        ]
        run([sys.executable, "-m", "compileall", *compile_targets], cwd=repo_root)

    checklist = """
Upload checklist
================
1. Inspect the generated dist/ archive and test the plugin in QGIS if needed.
2. Review the repository status:
     git status
3. Stage your changes:
     git add <files>
4. Commit onto main:
     git commit -m "Describe your change"
5. Push to your fork or origin:
     git push origin main
""".strip()
    print()
    print(checklist)


if __name__ == "__main__":
    main()
