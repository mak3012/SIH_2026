import hashlib
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import fetch

PAYLOAD = bytes(range(256)) * 4000  # ~1 MB
SHA = hashlib.sha256(PAYLOAD).hexdigest()


class Handler(BaseHTTPRequestHandler):
    requests: list[str | None] = []
    body = PAYLOAD

    def do_GET(self) -> None:  # noqa: N802
        rng = self.headers.get("Range")
        Handler.requests.append(rng)
        start = int(rng.split("=")[1].rstrip("-")) if rng else 0
        chunk = self.body[start:]
        self.send_response(206 if rng else 200)
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        self.wfile.write(chunk)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def server() -> Iterator[str]:
    Handler.requests, Handler.body = [], PAYLOAD
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/file.bin"
    httpd.shutdown()


@pytest.fixture
def raw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(fetch, "RAW", tmp_path)
    return tmp_path


def test_resumes_from_partial_download(server: str, raw: Path) -> None:
    dest = raw / "src" / "file.bin"
    dest.parent.mkdir()
    (dest.parent / "file.bin.part").write_bytes(PAYLOAD[:300_000])  # an interrupted run
    lock = raw / "lock.json"
    entry = fetch.fetch(server, dest, lock=lock)
    assert Handler.requests == ["bytes=300000-"]
    assert dest.read_bytes() == PAYLOAD and entry.sha256 == SHA
    assert json.loads(lock.read_text())["src/file.bin"]["sha256"] == SHA
    assert json.loads((raw / "src" / "manifest.json").read_text())["src/file.bin"]["bytes"] == len(
        PAYLOAD
    )


def test_second_run_downloads_nothing(server: str, raw: Path) -> None:
    lock = raw / "lock.json"
    fetch.fetch(server, raw / "src" / "file.bin", lock=lock)
    fetch.fetch(server, raw / "src" / "file.bin", lock=lock)
    assert Handler.requests == [None]


def test_checksum_mismatch_is_rejected(server: str, raw: Path) -> None:
    with pytest.raises(fetch.ChecksumError):
        fetch.fetch(server, raw / "src" / "file.bin", sha256="0" * 64, lock=raw / "lock.json")
    assert not (raw / "src" / "file.bin").exists() and not (raw / "src" / "file.bin.part").exists()


def test_pinned_file_that_changes_upstream_is_rejected(server: str, raw: Path) -> None:
    lock = raw / "lock.json"
    dest = raw / "src" / "file.bin"
    fetch.fetch(server, dest, lock=lock)
    dest.unlink()
    Handler.body = PAYLOAD[::-1]
    with pytest.raises(fetch.ChecksumError):
        fetch.fetch(server, dest, lock=lock)


def test_sodir_column_lookup_accepts_both_header_styles() -> None:
    from download_sodir import column

    display = ["Wellbore", "Document type", "Document name", "Document URL"]
    machine = ["wlbName", "wlbDocumentType", "wlbDocumentName", "wlbDocumentUrl"]
    for header in (display, machine):
        assert column(header, "Wellbore", "wlbName") == 0
        assert column(header, "Document name", "wlbDocumentName") == 2
        assert column(header, "Document URL", "wlbDocumentUrl") == 3


def test_completion_report_sample_is_deterministic(tmp_path: Path) -> None:
    from download_sodir import completion_reports

    csv_path = tmp_path / "documents.csv"
    rows = ["wlbName,wlbDocumentType,wlbDocumentName,wlbDocumentUrl"]
    rows += [
        f"1/{i}-1,REPORTED BY LICENSEE,{i}_COMPLETION_REPORT,https://x/{i}.pdf" for i in range(9)
    ]
    rows += ["2/1-1,REPORTED BY LICENSEE,2_1_1_DRILLING_PROGRAM,https://x/p.pdf"]
    csv_path.write_text("﻿" + "\n".join(rows) + "\n", encoding="utf-8")
    picked = completion_reports(csv_path, 3)
    assert picked == completion_reports(csv_path, 3) and len(picked) == 3
    assert all("COMPLETION" in name and url.endswith(".pdf") for _, name, url in picked)
