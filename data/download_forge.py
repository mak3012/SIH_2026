"""Utah FORGE well 16A(78)-32 (DOE Geothermal Data Repository): daily drilling reports, surveys,
mud log, Pason data, drilling summary.

Usage: python download_forge.py [--with-timeseries]

Geothermal, not oil and gas, but a highly deviated well with complete daily reports, surveys
and time-based drilling data under an open licence: a public stand-in for DDR + real-time data.
"""

import argparse
import zipfile
from pathlib import Path
from urllib.parse import quote

from fetch import RAW, fetch, licence_note, write_register

ROOT = RAW / "forge"
GDR = "https://gdr.openei.org/files/"
# (submission, file name, doc_type if it is a document to register)
FILES = [
    (1283, "16A(78)-32_Daily_Reports.zip", None),
    (1283, "16A(78)-32 Final mud log.las", None),
    (1283, "16A(78)-32_Pason_Data.zip", None),
    (1283, "16A(78)-32 Survey.xlsx", None),
    (1283, "16A(78)-32 PVA.pdf", "survey_report"),
    (1283, "U OF U WELL No. FORGE 16A (78)-32 SURVEY FINAL.pdf", "survey_report"),
    (1283, "Composite-C (1).pdf", "ddr"),
    (1296, "Drilling Summary Well 16A(78)-32.pdf", "other"),
]
TIMESERIES = [(1283, "16A(78)-32_time_data_10s_intervals.csv", None)]  # 113 MB
LICENCE = [
    "Utah FORGE data on the DOE Geothermal Data Repository: Creative Commons Attribution 4.0",
    "(CC BY 4.0). Attribute: Utah FORGE / Energy & Geoscience Institute at the University of",
    "Utah, U.S. Department of Energy, https://gdr.openei.org/submissions/1283 and /1296.",
]
BLOCK = {"block_id": "US-UT-FORGE", "block_name": "Utah FORGE (public)"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--with-timeseries", action="store_true", help="add the 10 s CSV")
    args = parser.parse_args()

    licence_note("Utah FORGE", LICENCE)
    items: list[dict[str, object]] = []
    for sub, name, doc_type in FILES + (TIMESERIES if args.with_timeseries else []):
        dest = ROOT / str(sub) / name
        fetch(GDR + f"{sub}/" + quote(name), dest)
        if doc_type:
            items.append(item(dest, doc_type, name))

    # Daily reports come zipped; each PDF inside is one DDR.
    zipped = ROOT / "1283" / "16A(78)-32_Daily_Reports.zip"
    out = ROOT / "1283" / "daily_reports"
    with zipfile.ZipFile(zipped) as z:
        for member in z.namelist():
            target = (out / member).resolve()
            if not target.is_relative_to(out.resolve()):  # zip-slip guard
                raise ValueError(f"unsafe path in zip: {member}")
            if member.lower().endswith(".pdf"):
                if not target.exists():
                    z.extract(member, out)
                items.append(item(target, "ddr", Path(member).stem))
    write_register("forge", items)


def item(path: Path, doc_type: str, title: str) -> dict[str, object]:
    return {
        "path": path.resolve().relative_to(RAW.resolve()).as_posix(),
        "kind": "document",
        "doc_type": doc_type,
        "title": f"FORGE 16A(78)-32 {title}",
        "vintage": None,
        "source": "forge",
        "synthetic": False,
        "classification": "public",
        **BLOCK,
    }


if __name__ == "__main__":
    main()
