"""Read models for the web screens (api.screens) and the review queue order, end to end against
a database (TEST_DATABASE_URL): the demo seed plus the planned well PLAN-1 of
test_intelligence_api, with row-level security in force."""

import json
from pathlib import Path
from typing import Any

import psycopg
import pytest
from test_intelligence_api import headers, select, world  # noqa: F401  (module fixture)

from api.workbench import review_value


def test_offset_map_preview(world: dict[str, Any]) -> None:  # noqa: F811
    r = world["client"].get(
        f"/offsets/preview/{world['bore']}",
        params={"formation": "Barail", "surface_radius_m": 20000, "subsurface_radius_m": 15000},
        headers=headers(roles="superintendent"),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["formation"] == "Barail"
    active = next(w for w in body["wells"] if w["active"])
    assert active["well"] == "PLAN-1" and len(active["path"]) >= 2 and active["at_formation"]
    ranked = [w for w in body["wells"] if w["candidate"]]
    assert ranked, "candidates within 20 km"
    for w in ranked:
        c = w["candidate"]
        assert (
            c["factors"] and abs(sum(f["contribution"] for f in c["factors"]) - c["score"]) < 0.01
        )
        assert all(0 <= f["score"] <= 1 for f in c["factors"])
    assert all(w["evidence"] >= w["events"] for w in body["wells"])


def test_correlation_tracks(world: dict[str, Any]) -> None:  # noqa: F811
    assert select(world).status_code == 200
    r = world["client"].get(f"/correlation/{world['bore']}", headers=headers())
    assert r.status_code == 200, r.text
    body = r.json()
    active, *offsets = body["tracks"]
    assert active["well"] == "PLAN-1" and offsets
    assert body["datum"] in body["datum_choices"]
    assert [p["formation"] for p in active["prognosis"]] == ["Tipam", "Barail", "Kopili"]
    assert active["bit"]["md"] == pytest.approx(1944, abs=1)
    for t in offsets:
        assert t["tops"] == sorted(t["tops"], key=lambda x: x["md"])
        for e in t["events"]:
            assert e["tvdss"] is not None and e["type"] in ("loss", "kick", "stuck_pipe", "other")
    assert (
        world["client"]
        .get(f"/correlation/{world['bore']}", params={"datum": "Nowhere"}, headers=headers())
        .status_code
        == 422
    )


def test_history_and_envelope(world: dict[str, Any]) -> None:  # noqa: F811
    r = world["client"].get(f"/active/{world['bore']}/history", headers=headers())
    assert r.status_code == 200
    samples = r.json()["samples"]
    assert samples and samples[-1]["values"]["bit_md"] == pytest.approx(1944)
    r = world["client"].get(f"/active/{world['bore']}/envelope", headers=headers())
    assert r.status_code == 200, r.text
    td = r.json()["torque_drag"]
    assert len(td["depth_md"]) == len(td["pickup"]) == len(td["slackoff"])
    # Tripping out drags more than rotating, which drags more than tripping in.
    assert all(
        p >= rot >= s
        for p, rot, s in zip(td["pickup"], td["rotating"], td["slackoff"], strict=True)
    )
    assert td["pickup_hi"][-1] >= td["pickup"][-1]
    win = r.json()["pressure_window"]
    assert {w["formation"] for w in win} == {"Tipam", "Barail", "Kopili"}
    for w in win:
        if w["pore_min_sg"] and w["frac_max_sg"]:
            assert all(e["kind"] == "kick" for e in w["pore_evidence"])


def test_document_file_and_fields(
    world: dict[str, Any],  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    db_url: str,
) -> None:
    with psycopg.connect(db_url) as c:
        doc = c.execute(
            "INSERT INTO document (sha256, type, title, uri, page_count, block_id) VALUES"
            " (sha256('syn-1'::bytea), 'wcr', 'SYN-1 WCR (SYNTHETIC)',"
            " 'synthetic://SYN-1/SYN-1 WCR (SYNTHETIC)', 1, 'AA-ONN-2001/1') RETURNING id"
        ).fetchone()[0]  # type: ignore[index]
    from api import storage

    root = tmp_path / "raw"
    (root / "synthetic_assam" / "wells").mkdir(parents=True)
    (root / "synthetic_assam" / "wells" / "SYN-1.pdf").write_bytes(b"%PDF-1.4 test")
    (root / "synthetic_assam" / "documents.json").write_text(
        json.dumps(
            {
                "documents": [
                    {"title": "SYN-1 WCR (SYNTHETIC)", "path": "synthetic_assam/wells/SYN-1.pdf"}
                ]
            }
        )
    )
    monkeypatch.setenv("SYNTHETIC_RAW", str(root))
    storage._synthetic_index.cache_clear()
    storage.read.cache_clear()
    r = world["client"].get(f"/documents/{doc}/file", headers=headers())
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert r.headers["content-type"] == "application/pdf"
    with psycopg.connect(db_url) as c:  # the read is in the audit log, with who read it
        read = c.execute(
            "SELECT actor FROM audit_log WHERE action = 'read.document' AND entity_id = %s",
            (str(doc),),
        ).fetchall()
    assert read == [("tester",)]
    seed_doc = world["client"].get("/review/items", headers=headers())  # endpoint is reachable
    assert seed_doc.status_code == 200
    with psycopg.connect(db_url) as c:
        wcr = c.execute(
            "SELECT d.id FROM document d JOIN source_ref r ON r.document_id = d.id"
            " JOIN formation_top t ON t.source_ref_id = r.id LIMIT 1"
        ).fetchone()[0]  # type: ignore[index]
    r = world["client"].get(f"/documents/{wcr}/fields", headers=headers())
    assert r.status_code == 200, r.text
    facts = r.json()["facts"]
    assert any(f["field"] == "formation_top" and f["unit"] == "m MD" for f in facts)
    hidden = world["client"].get(f"/documents/{wcr}/fields", headers=headers(blocks="NONE"))
    assert hidden.status_code == 404


def test_fleet_board(world: dict[str, Any]) -> None:  # noqa: F811
    r = world["client"].get("/fleet", headers=headers(roles="superintendent"))
    assert r.status_code == 200, r.text
    rig = next(x for x in r.json()["rigs"] if x["well"] == "PLAN-1")
    assert rig["current_md"] == pytest.approx(1944)
    assert rig["rate_m_per_hr"] == pytest.approx(12, rel=0.01)
    if rig["next_hazard"] and rig["next_hazard"]["state"] == "ahead":
        assert rig["next_hazard"]["eta_hours"] == pytest.approx(
            rig["next_hazard"]["distance_m"] / 12, abs=0.1
        )
    assert set(r.json()["digest"]["by_tier"]) == {"advisory", "warning", "critical"}


def test_voice_note_capture(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    from api import storage

    stored: list[str] = []
    monkeypatch.setattr(storage, "put", lambda b, k, d, t: stored.append(k) or f"s3://{b}/{k}")
    params = {
        "wellbore_id": world["bore"],
        "client_id": "device-0001-note-a",
        "duration_s": 12.5,
        "formation": "barail",
        "note": "tight at the coal",
    }
    up = {"content-type": "audio/webm", **headers()}
    r = world["client"].post("/voice-notes", params=params, content=b"\x1aE\xdf\xa3abc", headers=up)
    assert r.status_code == 201, r.text
    first = r.json()
    assert first["status"] == "pending_confirmation" and not first["duplicate"]
    again = world["client"].post("/voice-notes", params=params, content=b"other", headers=up)
    assert again.json() == {**first, "duplicate": True} and len(stored) == 1
    mine = world["client"].get("/voice-notes", headers=headers()).json()
    note = next(n for n in mine if n["id"] == first["id"])
    assert note["formation"] == "Barail" and note["status"] == "transcribing"
    planner = {"content-type": "audio/webm", **headers(roles="planner")}
    other = {**params, "client_id": "device-0001-note-b"}
    assert (
        world["client"]
        .post("/voice-notes", params=other, content=b"x", headers=planner)
        .status_code
        == 403
    )
    bad = {"content-type": "text/plain", **headers()}
    assert (
        world["client"].post("/voice-notes", params=other, content=b"x", headers=bad).status_code
        == 415
    )


def test_review_value_order() -> None:
    assert review_value("casing_shoe_md", 0.5, 0) > review_value("well_name", 0.5, 0)
    assert review_value("mw", 0.4, 1) > review_value("mw", 0.4, 0) > review_value("mw", 0.9, 0)
    assert review_value("anything", 1.0, 3) == 0


def test_ask_stream_events(world: dict[str, Any]) -> None:  # noqa: F811
    with world["client"].stream(
        "POST",
        "/assistant/ask/stream",
        json={"question": "What is the Barail top in DEMO-1?"},
        headers=headers(),
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        events = [
            (block.split("\n")[0][7:], json.loads(block.split("\n")[1][6:]))
            for block in r.read().decode().strip().split("\n\n")
        ]
    kinds = [k for k, _ in events]
    assert kinds[0] == "status" and kinds[-1] == "done"
    done = events[-1][1]
    assert "".join(d["text"] for k, d in events if k == "delta") == done["answer"]
    assert done["conversation_id"]
