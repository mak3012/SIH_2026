"""python -m synthetic_assam: see synthetic_assam/cli.py."""

from synthetic_assam.cli import main

if __name__ == "__main__":  # guard: worker processes re-import this module on Windows
    main()
