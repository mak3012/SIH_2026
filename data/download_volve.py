"""Equinor Volve field data (well technical data and reports).

Usage: python download_volve.py [--prefix Well_technical_data/ ...] [--max-gb 2]

Volve access needs a personal acceptance of the Equinor Open Data Licence, so this script
cannot fetch anonymously. Give it one of:
  VOLVE_SAS_URL  container URL with SAS token from the Equinor access flow
                 (https://www.equinor.com/energy/volve-data-sharing)
  VOLVE_DIR      a folder you already downloaded (files are copied and checksummed)
With neither, it prints the licence and how to get access, and exits 0 so `make seed` goes on.
"""

import argparse
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from fetch import RAW, UA, fetch, licence_note, write_register

ROOT = RAW / "volve"
LICENCE = [
    "Equinor Open Data Licence (Volve). Granted to academic institutions, students and",
    "researchers; read the licence text in the Equinor access guide before any other use.",
    "Attribute Equinor and the Volve licence partners (ExxonMobil E&P Norway, Bayerngas Norge).",
    "https://www.equinor.com/energy/volve-data-sharing",
]
BLOCK = {"block_id": "NO-VOLVE-PL046", "block_name": "Volve, PL046 (Equinor open data)"}


def doc_type(name: str) -> str:
    n = name.lower()
    return "wcr" if "completion" in n else "ddr" if "daily" in n else "other"


def list_blobs(sas_url: str, prefix: str) -> list[tuple[str, int]]:
    base = urlsplit(sas_url)
    out, marker = [], ""
    while True:
        query = f"{base.query}&restype=container&comp=list&prefix={quote(prefix)}&marker={marker}"
        url = urlunsplit(base._replace(query=query))
        with urlopen(Request(url, headers={"User-Agent": UA}), timeout=60) as r:
            root = ET.fromstring(r.read())
        for blob in root.iter("Blob"):
            size = int(blob.findtext("Properties/Content-Length") or 0)
            out.append((blob.findtext("Name") or "", size))
        marker = root.findtext("NextMarker") or ""
        if not marker:
            return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--prefix", nargs="+", default=["Well_technical_data/", "Reports/"])
    parser.add_argument("--max-gb", type=float, default=2.0, help="stop before exceeding this")
    args = parser.parse_args()
    licence_note("Equinor Volve", LICENCE)

    sas, local = os.environ.get("VOLVE_SAS_URL"), os.environ.get("VOLVE_DIR")
    if sas:
        base = urlsplit(sas)
        blobs = [b for p in args.prefix for b in list_blobs(sas, p)]
        sources = [
            (urlunsplit(base._replace(path=f"{base.path}/{quote(n)}")), n, s) for n, s in blobs
        ]
    elif local:
        root = Path(local)
        files = [f for p in args.prefix for f in sorted((root / p).rglob("*")) if f.is_file()]
        sources = [(f.as_uri(), f.relative_to(root).as_posix(), f.stat().st_size) for f in files]
    else:
        print("  skipped: set VOLVE_SAS_URL or VOLVE_DIR after accepting the licence (see above)")
        return

    budget = args.max_gb * 1e9
    items: list[dict[str, object]] = []
    for url, name, size in sorted(sources, key=lambda s: s[1]):
        if size > budget:
            print(f"  stopping at --max-gb {args.max_gb}; next was {name}")
            break
        budget -= size
        dest = ROOT / name
        fetch(url, dest)
        if name.lower().endswith(".pdf"):
            items.append(
                {
                    "path": dest.relative_to(RAW).as_posix(),
                    "kind": "document",
                    "doc_type": doc_type(name),
                    "title": f"Volve {Path(name).stem}",
                    "vintage": None,
                    "source": "volve",
                    "synthetic": False,
                    "classification": "public",
                    **BLOCK,
                }
            )
    write_register("volve", items)


if __name__ == "__main__":
    main()
