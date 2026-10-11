"""`make seed` for data/: download public sources, generate synthetic data, register it all.

Usage: python seed.py [--offline] [--wells N]

Each step runs as its own process so one failing source does not stop the others; the exit
code is non-zero if any step failed, and the failures are listed at the end.
--offline skips the three downloaders (the synthetic generator still needs its fonts once).
"""

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--offline", action="store_true", help="skip public-data downloads")
    p.add_argument("--wells", type=int, default=None, help="synthetic wells (default: config)")
    args = p.parse_args()

    py = sys.executable
    steps: list[tuple[str, list[str]]] = []
    if not args.offline:
        steps += [
            ("Sodir FactPages", [py, "download_sodir.py"]),
            ("Utah FORGE", [py, "download_forge.py"]),
            ("Equinor Volve", [py, "download_volve.py"]),
        ]
    wells = ["--wells", str(args.wells)] if args.wells else []
    steps += [
        ("synthetic Upper Assam wells", [py, "-m", "synthetic_assam", *wells]),
        ("synthetic real-time CSV", [py, "synthetic/generate.py"]),
        ("register in the object store + Postgres", [py, "register.py"]),
    ]
    failed = []
    for name, cmd in steps:
        print(f"\n== {name}", flush=True)
        if subprocess.run(cmd, cwd=HERE).returncode != 0:
            failed.append(name)
    if failed:
        print(f"\nseed finished with failures: {', '.join(failed)}", file=sys.stderr)
        sys.exit(1)
    print("\nseed finished")


if __name__ == "__main__":
    main()
