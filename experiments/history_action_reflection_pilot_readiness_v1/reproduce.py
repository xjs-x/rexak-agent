"""Reproduce offline runs in a NEW directory without overwriting frozen data."""

from __future__ import annotations

import argparse
import asyncio
import shutil
from pathlib import Path

from . import rehearsal
from .protocol import ROOT


def main():
    parser = argparse.ArgumentParser(description="Offline mocks only; no live execution mode")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise FileExistsError("output_must_be_a_new_directory")
    output.mkdir(parents=True)
    (output / "frozen").mkdir()
    for name in (
        "tasks.json",
        "schedule.json",
        "contract.json",
        "notice.json",
        "production-prompt.json",
        "tool-schemas.json",
    ):
        shutil.copyfile(ROOT / "frozen" / name, output / "frozen" / name)
    rehearsal.ROOT = output
    asyncio.run(rehearsal.main())


if __name__ == "__main__":
    main()
