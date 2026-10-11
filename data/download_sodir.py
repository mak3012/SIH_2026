"""Norwegian Offshore Directorate (Sodir) FactPages: wellbores, formation tops, casing + LOT,
document metadata, and a sample of scanned completion-report PDFs.

Usage: python download_sodir.py [--sample 10] [--refresh]

Tables change daily, so they are stored as dated snapshots (raw/sodir/tables/YYYY-MM-DD/) and
checksummed in the manifest but not pinned. The latest snapshot is reused unless --refresh.
PDFs are static and pinned in checksums.lock.json.
"""

import argparse
import csv
import random
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from fetch import RAW, fetch, licence_note, write_register

ROOT = RAW / "sodir"
EXPORT = (
    "https://factpages.sodir.no/public?/Factpages/external/tableview/{table}"
    "&rs:Command=Render&rc:Toolbar=false&rc:Parameters=f&IpAddress=not_used"
    "&CultureCode=en&rs:Format=CSV&Top100=false"
)
TABLES = {
    "wellbores": "wellbore_all_long",
    "formation_tops": "wellbore_formation_top",
    "casing_lot": "wellbore_casing_and_lot",
    "documents": "wellbore_document",
}
LICENCE = [
    "Sodir FactPages data: Norwegian Licence for Open Government Data (NLOD 2.0).",
    "Attribute as: 'Contains data from the Norwegian Offshore Directorate (Sodir)'.",
    "Wellbore documents (PDF) are reported by licensees and published by Sodir. They are",
    "used here for research and evaluation; check terms before redistributing them.",
    "https://www.sodir.no/en/about-us/copyright-and-use-of-data/",
]


def column(header: list[str], *names: str) -> int:
    """Index of the first of `names` present (case-insensitive). The export has used both
    display headers ("Document URL") and machine names ("wlbDocumentUrl")."""
    lower = [h.strip().lower() for h in header]
    for n in names:
        if n.lower() in lower:
            return lower.index(n.lower())
    raise KeyError(f"none of {names} in {header}")


def snapshot(refresh: bool) -> Path:
    tables = ROOT / "tables"
    existing = sorted(p for p in tables.glob("*") if p.is_dir()) if tables.exists() else []
    if existing and not refresh:
        print(f"  reusing snapshot {existing[-1].name} (use --refresh for a new one)")
        return existing[-1]
    day = tables / date.today().isoformat()
    for name, table in TABLES.items():
        fetch(EXPORT.format(table=table), day / f"{name}.csv", pin=False)
    return day


def completion_reports(documents_csv: Path, n: int) -> list[tuple[str, str, str]]:
    """Deterministic sample of (wellbore, document name, url) for completion reports."""
    with documents_csv.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    header, body = rows[0], rows[1:]
    wb = column(header, "Wellbore", "Wellbore name", "wlbName")
    name = column(header, "Document name", "wlbDocumentName")
    url = column(header, "Document URL", "wlbDocumentUrl")
    hits = sorted(
        (r[wb], r[name], r[url])
        for r in body
        if len(r) > url and "completion" in r[name].lower() and r[url].lower().endswith(".pdf")
    )
    return random.Random(0).sample(hits, min(n, len(hits)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sample", type=int, default=10, help="completion-report PDFs to fetch")
    parser.add_argument("--refresh", action="store_true", help="take a new table snapshot")
    args = parser.parse_args()

    licence_note("Sodir FactPages", LICENCE)
    day = snapshot(args.refresh)
    items: list[dict[str, object]] = []
    for wellbore, name, url in completion_reports(day / "documents.csv", args.sample):
        dest = ROOT / "documents" / Path(urlsplit(url).path).name
        fetch(url, dest)
        items.append(
            {
                "path": dest.relative_to(RAW).as_posix(),
                "kind": "document",
                "doc_type": "wcr",
                "title": f"{wellbore} {name}",
                "vintage": None,
                "source": "sodir",
                "synthetic": False,
                "classification": "public",
                "block_id": "NO-SODIR",
                "block_name": "Norwegian Continental Shelf (Sodir public)",
            }
        )
    write_register("sodir", items)


if __name__ == "__main__":
    main()
