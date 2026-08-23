from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


MIN_CYCLES = 25
MAX_CYCLES = 10_000
DEFAULT_CYCLES = 1_000


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run deterministic Aruba Mini Dashboard fault-injection soak tests."
    )
    parser.add_argument(
        "--cycles",
        type=int,
        default=DEFAULT_CYCLES,
        help=f"repeat count per soak scenario ({MIN_CYCLES}-{MAX_CYCLES})",
    )
    args = parser.parse_args(argv)
    if not MIN_CYCLES <= args.cycles <= MAX_CYCLES:
        parser.error(f"--cycles must be between {MIN_CYCLES} and {MAX_CYCLES}")

    repository = Path(__file__).resolve().parent.parent
    environment = os.environ.copy()
    environment["ARUBA_RELIABILITY_CYCLES"] = str(args.cycles)
    environment.setdefault("PYTHONUTF8", "1")
    environment.setdefault("QT_QPA_PLATFORM", "offscreen")
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-m",
        "reliability",
        "--maxfail=1",
    ]
    print(
        f"Running deterministic reliability soak with {args.cycles} cycles per scenario...",
        flush=True,
    )
    completed = subprocess.run(
        command,
        cwd=repository,
        env=environment,
        check=False,
    )
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
