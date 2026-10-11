"""Resumable, checksummed, idempotent HTTP downloads (stdlib only).

- Resumable: bytes land in `<dest>.part`; a rerun asks for the rest with an HTTP Range header.
- Checksummed: every finished file's sha256 is recorded in the source's manifest.json. Files
  fetched with `pin=True` are also pinned in data/checksums.lock.json (commit it): a later
  download that does not match the pin is rejected.
- Idempotent: a file already on disk whose sha256 matches its pin/manifest is not fetched again.
"""

import hashlib
import json
import shutil
import sys
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DATA = Path(__file__).resolve().parent
RAW = DATA / "raw"
LOCK = DATA / "checksums.lock.json"
UA = "well-intelligence-datatools/0.1 (+research use)"


class ChecksumError(Exception):
    pass


@dataclass
class Entry:
    path: str  # relative to data/raw
    url: str
    sha256: str
    bytes: int
    fetched_at: str


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _load(path: Path) -> dict[str, dict[str, object]]:
    return json.loads(path.read_text()) if path.exists() else {}


def _save(path: Path, data: dict[str, dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def _rel(dest: Path) -> str:
    return dest.resolve().relative_to(RAW.resolve()).as_posix()


def fetch(
    url: str,
    dest: Path,
    *,
    sha256: str | None = None,
    pin: bool = True,
    attempts: int = 4,
    lock: Path = LOCK,
) -> Entry:
    """Download `url` to `dest` (under data/raw), resuming and verifying. Returns its entry.

    `sha256` wins over the lock file. With neither, the first download is trusted and, if
    `pin`, recorded (trust on first use).
    """
    rel = _rel(dest)
    manifest_path = RAW / rel.split("/")[0] / "manifest.json"
    manifest = _load(manifest_path)
    pins = _load(lock)
    expected = sha256 or (str(pins[rel]["sha256"]) if rel in pins else None)

    if dest.exists():
        have = sha256_of(dest)
        known = expected or manifest.get(rel, {}).get("sha256")
        if known is None or have == known:
            print(f"  ok      {rel}")
            return _record(manifest_path, manifest, lock, pins, rel, url, dest, have, pin)
        print(f"  stale   {rel} (checksum changed), downloading again")
        dest.unlink()

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    for attempt in range(1, attempts + 1):
        try:
            _download(url, part)
            break
        except (URLError, TimeoutError, ConnectionError) as e:
            if attempt == attempts:
                raise
            wait = 2**attempt
            print(f"  retry   {rel} in {wait}s ({e})", file=sys.stderr)
            time.sleep(wait)

    have = sha256_of(part)
    if expected and have != expected:
        part.unlink()
        raise ChecksumError(f"{rel}: sha256 {have} != expected {expected}")
    part.replace(dest)
    print(f"  fetched {rel} ({dest.stat().st_size:,} bytes)")
    return _record(manifest_path, manifest, lock, pins, rel, url, dest, have, pin)


def _download(url: str, part: Path) -> None:
    start = part.stat().st_size if part.exists() else 0
    headers = {"User-Agent": UA}
    if start:
        headers["Range"] = f"bytes={start}-"
    try:
        resp = urlopen(Request(url, headers=headers), timeout=60)
    except HTTPError as e:
        if e.code == 416 and start:  # already have every byte
            return
        raise
    with resp:
        resumed = start and resp.status == 206
        with part.open("ab" if resumed else "wb") as f:
            shutil.copyfileobj(resp, f, 1 << 20)


def _record(
    manifest_path: Path,
    manifest: dict[str, dict[str, object]],
    lock: Path,
    pins: dict[str, dict[str, object]],
    rel: str,
    url: str,
    dest: Path,
    digest: str,
    pin: bool,
) -> Entry:
    entry = Entry(rel, url, digest, dest.stat().st_size, datetime.now(UTC).isoformat())
    if manifest.get(rel, {}).get("sha256") != digest:
        manifest[rel] = asdict(entry)
        _save(manifest_path, manifest)
    if pin and rel not in pins:
        pins[rel] = {"sha256": digest, "url": url}
        _save(lock, pins)
    return entry


def write_register(source: str, items: list[dict[str, object]]) -> Path:
    """What `register.py` should load into the object store + Postgres for this source.

    Each item: path (relative to data/raw), kind ('document' | 'audio'), title, source,
    synthetic, classification, block_id, block_name; documents add doc_type and vintage;
    audio adds duration_s, speaker, lang.
    """
    path = RAW / source / "register.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(items, key=lambda i: str(i["path"])), indent=2) + "\n")
    print(f"  {len(items)} items listed for registration in {_rel(path)}")
    return path


def licence_note(source: str, lines: Iterable[str]) -> None:
    bar = "-" * 78
    print(f"{bar}\n{source}: licence and terms of use")
    for line in lines:
        print(f"  {line}")
    print(bar)
