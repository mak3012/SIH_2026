"""Daily sync of the Sodir FactPages (Norwegian Offshore Directorate, open data, NLOD 2.0).

1. Exploration wellbores (location, purpose, status, dates, depth, operator) are upserted into
   `public_well`; a wellbore not seen before keeps `first_seen` = now, so the map marks it new.
2. Completion reports of the most recently spudded wellbores not checked yet are downloaded
   (a few per run: they are 5-150 MB scans), stored in the object store and registered as
   documents; the `new_documents` sensor then runs them through extraction.

Needs outbound HTTPS to factpages.sodir.no (laptop: the dev overlay; headquarters: allow that
host in the egress firewall). Attribution: "Contains data from the Norwegian Offshore
Directorate (Sodir)".
"""

import csv
import hashlib
import http.client
import io
import ssl
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import certifi
import psycopg

EXPORT = (
    "https://factpages.sodir.no/public?/Factpages/external/tableview/{table}"
    "&rs:Command=Render&rc:Toolbar=false&rc:Parameters=f&IpAddress=not_used"
    "&CultureCode=en&rs:Format=CSV&Top100=false"
)
BLOCK = "NO-SODIR"
SOURCE = "sodir"
UA = {"User-Agent": "well-intelligence/1.0 (daily open-data sync)"}


def get(url: str, timeout: float = 300, tries: int = 4) -> bytes:
    """GET with retries (5, 20, 45 s apart): a dropped connection should not cost a day."""
    # Public CA bundle: inside the stack SSL_CERT_FILE trusts only the stack's own CA.
    tls = ssl.create_default_context(cafile=certifi.where())
    req = urllib.request.Request(url, headers=UA)
    for n in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=tls) as r:
                return bytes(r.read())
        except (OSError, http.client.HTTPException) as e:  # URLError, SSL, timeouts, resets
            if n == tries - 1 or (isinstance(e, urllib.error.HTTPError) and e.code < 500):
                raise
            time.sleep(5 * (n + 1) ** 2)
    raise AssertionError("unreachable")


def table(name: str, fetch: Callable[[str], bytes] = get) -> list[dict[str, str]]:
    text = fetch(EXPORT.format(table=name)).decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def _date(s: str) -> date | None:
    """Sodir dates are dd.mm.yyyy."""
    try:
        d, m, y = s.strip().split(".")
        return date(int(y), int(m), int(d))
    except ValueError:
        return None


def _num(s: str) -> float | None:
    try:
        return float(s)
    except ValueError:
        return None


@dataclass(frozen=True)
class Wellbore:
    id: int
    name: str
    lat: float
    lon: float
    area: str
    purpose: str
    status: str
    content: str
    operator: str
    licence: str
    field: str
    entry_date: date | None
    completion_date: date | None
    total_depth_m: float | None
    water_depth_m: float | None
    formation_at_td: str
    factpage_url: str
    press_release_url: str
    source_updated: date | None


def wellbores(rows: Iterable[dict[str, str]]) -> list[Wellbore]:
    """Rows of the `wellbore_exploration_all` table with a location."""
    out = []
    for r in rows:
        lat, lon = _num(r.get("wlbNsDecDeg", "")), _num(r.get("wlbEwDecDeg", ""))
        npdid = r.get("wlbNpdidWellbore", "").strip()
        if lat is None or lon is None or not npdid.isdigit():
            continue
        out.append(
            Wellbore(
                id=int(npdid),
                name=r["wlbWellboreName"].strip(),
                lat=lat,
                lon=lon,
                area=r.get("wlbMainArea", "").strip(),
                purpose=r.get("wlbPurpose", "").strip(),
                status=r.get("wlbStatus", "").strip(),
                content=r.get("wlbContent", "").strip(),
                operator=r.get("wlbDrillingOperator", "").strip(),
                licence=r.get("wlbProductionLicence", "").strip(),
                field=r.get("wlbField", "").strip(),
                entry_date=_date(r.get("wlbEntryDate", "")),
                completion_date=_date(r.get("wlbCompletionDate", "")),
                total_depth_m=_num(r.get("wlbTotalDepth", "")),
                water_depth_m=_num(r.get("wlbWaterDepth", "")),
                formation_at_td=r.get("wlbFormationAtTd", "").strip(),
                factpage_url=r.get("wlbFactPageUrl", "").strip(),
                press_release_url=r.get("wlbPressReleaseUrl", "").strip(),
                source_updated=_date(r.get("wlbDateUpdated", "")),
            )
        )
    return out


def upsert(conn: psycopg.Connection, wells: list[Wellbore]) -> tuple[int, int]:
    """Insert or update every wellbore. Returns (new, total)."""
    before = conn.execute("SELECT count(*) FROM public_well WHERE source = %s", (SOURCE,))
    n0 = before.fetchone()[0]  # type: ignore[index]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO public_well (id, source, name, block_id, surface, area, purpose, status,"
            " content, operator, licence, field, entry_date, completion_date, total_depth_m,"
            " water_depth_m, formation_at_td, factpage_url, press_release_url, source_updated)"
            " VALUES (%s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s, %s, %s, %s,"
            " %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, surface = EXCLUDED.surface,"
            " area = EXCLUDED.area, purpose = EXCLUDED.purpose, status = EXCLUDED.status,"
            " content = EXCLUDED.content, operator = EXCLUDED.operator,"
            " licence = EXCLUDED.licence, field = EXCLUDED.field,"
            " entry_date = EXCLUDED.entry_date, completion_date = EXCLUDED.completion_date,"
            " total_depth_m = EXCLUDED.total_depth_m, water_depth_m = EXCLUDED.water_depth_m,"
            " formation_at_td = EXCLUDED.formation_at_td, factpage_url = EXCLUDED.factpage_url,"
            " press_release_url = EXCLUDED.press_release_url,"
            " source_updated = EXCLUDED.source_updated, synced_at = now()",
            [
                (
                    w.id,
                    SOURCE,
                    w.name,
                    BLOCK,
                    w.lon,
                    w.lat,
                    w.area,
                    w.purpose,
                    w.status,
                    w.content,
                    w.operator,
                    w.licence,
                    w.field,
                    w.entry_date,
                    w.completion_date,
                    w.total_depth_m,
                    w.water_depth_m,
                    w.formation_at_td,
                    w.factpage_url,
                    w.press_release_url,
                    w.source_updated,
                )  # fmt: skip
                for w in wells
            ],
        )
    n1 = conn.execute("SELECT count(*) FROM public_well WHERE source = %s", (SOURCE,))
    total = n1.fetchone()[0]  # type: ignore[index]
    return total - n0, total


def completion_reports(rows: Iterable[dict[str, str]]) -> dict[int, list[tuple[str, str, int]]]:
    """NPDID -> [(document name, url, size in KB)] of completion reports (not logs)."""
    out: dict[int, list[tuple[str, str, int]]] = {}
    for r in rows:
        name, url = r.get("wlbDocumentName", ""), r.get("wlbDocumentUrl", "")
        npdid = r.get("wlbNpdidWellbore", "").strip()
        if "COMPLETION_REPORT" in name.upper() and url.lower().endswith(".pdf") and npdid.isdigit():
            size = int(_num(r.get("wlbDocumentSize", "")) or 0)
            out.setdefault(int(npdid), []).append((name, url, size))
    return out


Put = Callable[[str, Path, str], None]  # (object key, file, sha256)


def fetch_reports(
    conn: psycopg.Connection,
    reports: dict[int, list[tuple[str, str, int]]],
    put: Put,
    limit: int,
    max_kb: int,
    fetch: Callable[[str], bytes] = get,
) -> list[str]:
    """Download, store and register the completion reports of up to `limit` wellbores: those
    with a report published (within `max_kb`) and not fetched yet, most recently spudded first.
    A wellbore still drilling has no report; it is picked up once Sodir publishes one.
    Returns the registered titles."""
    ready = [n for n, docs in reports.items() if any(kb <= max_kb for _, _, kb in docs)]
    todo = conn.execute(
        "SELECT id, name FROM public_well WHERE source = %s AND reports_checked_at IS NULL"
        " AND id = ANY(%s) ORDER BY entry_date DESC NULLS LAST, id DESC LIMIT %s",
        (SOURCE, ready, limit),
    ).fetchall()
    titles = []
    for npdid, name in todo:
        for doc, url, kb in reports.get(npdid, []):
            if kb > max_kb:
                continue
            data = fetch(url)
            sha = hashlib.sha256(data).hexdigest()
            key = f"sodir/documents/{Path(url).name}"
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / Path(url).name
                path.write_bytes(data)
                put(key, path, sha)
            cur = conn.execute(
                "INSERT INTO document (sha256, type, title, classification, uri, block_id,"
                " source, synthetic) VALUES (%s, 'wcr', %s, 'public', %s, %s, %s, false)"
                " ON CONFLICT (sha256) DO NOTHING",
                (bytes.fromhex(sha), f"{name} {doc}", f"s3://documents/{key}", BLOCK, SOURCE),
            )
            if cur.rowcount:
                titles.append(f"{name} {doc}")
        conn.execute("UPDATE public_well SET reports_checked_at = now() WHERE id = %s", (npdid,))
    return titles
