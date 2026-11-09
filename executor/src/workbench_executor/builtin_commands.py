"""Provision ordinary Executor commands from an already installed toolchain.

Run on Kali with python -m workbench_executor.builtin_commands and the two roots.
This does not install packages,
change Python environments, or restart services. Existing different files are
preserved and rejected. No task modes or task-specific configuration is created.
"""

from __future__ import annotations

import argparse
import os
import shlex
from pathlib import Path


def provision(data_root: Path, toolchain_root: Path) -> list[Path]:
    if not data_root.is_absolute() or not toolchain_root.is_absolute():
        raise ValueError("data-root and toolchain-root must be absolute paths")
    toolchain = toolchain_root.resolve(strict=True)
    commands = {
        "gdb": [toolchain / "bin/gdb"],
        "checksec": [toolchain / "bin/checksec"],
        "patchelf": [toolchain / "bin/patchelf"],
        "ROPgadget": [toolchain / "venv/bin/ROPgadget"],
        "pwn-python": [toolchain / "venv/bin/python3"],
        "gef": [toolchain / "bin/gdb", "-ex", f"source {toolchain}/sysroot/usr/share/gdb/gef.py"],
    }
    for argv in commands.values():
        target = Path(argv[0])
        if not target.is_file() or not os.access(target, os.X_OK):
            raise ValueError(f"Missing executable: {target}")
    if not (toolchain / "sysroot/usr/share/gdb/gef.py").is_file():
        raise ValueError("Missing GEF script")
    directory = data_root / "tools/bin"
    contents = {
        directory / name: "#!/bin/sh\nexec " + " ".join(shlex.quote(str(arg)) for arg in argv) + ' "$@"\n'
        for name, argv in commands.items()
    }
    # Preflight all names before writing any entry; never overwrite user tools.
    for path, content in contents.items():
        if path.is_symlink() or (path.exists() and path.read_text(encoding="utf-8") != content):
            raise ValueError(f"Existing command differs; preserved: {path}")
    directory.mkdir(parents=True, exist_ok=True)
    for path, content in contents.items():
        if not path.exists():
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
        path.chmod(0o755)
    return list(contents)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--toolchain-root", type=Path, required=True)
    args = parser.parse_args()
    for path in provision(args.data_root, args.toolchain_root):
        print(path)


if __name__ == "__main__":
    main()
