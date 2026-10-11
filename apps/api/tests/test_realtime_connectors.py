"""Connectors against real protocol endpoints in-process: WITS0 over TCP, OPC-UA (asyncua
server), the ETP stub, and SQL polling (database)."""

import asyncio
import socket
from datetime import UTC, datetime

import psycopg
import pytest

from api.realtime.connectors import Etp12Client, OpcUaClient, SqlPoller, Wits0Listener


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_wits0_listener_over_tcp() -> None:
    async def go() -> list:
        lst = Wits0Listener(5, host="127.0.0.1", port=0, units={"spp": "psi", "bit_md": "ft"})
        port = await lst.start()
        _, w = await asyncio.open_connection("127.0.0.1", port)
        # Two frames, the second split across writes as a serial converter may do.
        w.write(b"&&\r\n0105260301\r\n0106063000\r\n0108 1000\r\n0122 2000\r\n!!\r\n&&\r\n01")
        await w.drain()
        w.write(b"08 1001\r\n!!\r\n")
        await w.drain()
        out = [await asyncio.wait_for(anext(aiter(lst.stream())), 5) for _ in range(2)]
        w.close()
        assert lst.server is not None
        lst.server.close()
        return out

    a, b = asyncio.run(go())
    assert a.wellbore_id == 5 and a.ts == datetime(2026, 3, 1, 6, 30, tzinfo=UTC)
    assert a.values == pytest.approx({"bit_md": 304.8, "spp": 137.895}, rel=1e-4)
    assert b.values == pytest.approx({"bit_md": 1001 * 0.3048})


def test_opcua_client_reads_nodes_with_units() -> None:
    from asyncua import Server

    port = free_port()

    async def go() -> list:
        server = Server()
        await server.init()
        server.set_endpoint(f"opc.tcp://127.0.0.1:{port}/rig/")
        ns = await server.register_namespace("urn:rig")
        rig = await server.nodes.objects.add_object(ns, "Rig")
        hkld = await rig.add_variable(ns, "Hookload", 200.0)  # klbf
        spp = await rig.add_variable(ns, "SPP", 15000.0)  # kPa
        async with server:
            client = OpcUaClient(
                9,
                f"opc.tcp://127.0.0.1:{port}/rig/",
                {"hookload": hkld.nodeid.to_string(), "spp": spp.nodeid.to_string()},
                {"hookload": "klbf", "spp": "kPa"},
                interval_s=0.2,
            )
            stream = client.stream()
            first = await asyncio.wait_for(anext(stream), 10)
            await hkld.write_value(210.0)
            second = await asyncio.wait_for(anext(stream), 10)
            await stream.aclose()
            return [first, second]

    first, second = asyncio.run(go())
    assert first.values == pytest.approx({"hookload": 889.644, "spp": 150.0}, rel=1e-4)
    assert second.values["hookload"] == pytest.approx(210 * 4.44822, rel=1e-4)
    assert second.ts > first.ts


def test_etp_is_an_explicit_stub() -> None:
    with pytest.raises(NotImplementedError):
        Etp12Client(1, "wss://example/etp", []).stream()


def test_sql_poller(db_url: str) -> None:
    with psycopg.connect(db_url, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS historian")
        c.execute("CREATE TABLE historian (ts timestamptz, dbtm real, sppa real)")
        c.execute(
            "INSERT INTO historian VALUES ('2026-03-01T06:30:00Z', 1000, 2000),"
            " ('2026-03-01T06:30:05Z', 1001, 2010)"
        )
    q = "SELECT ts, dbtm, sppa FROM historian WHERE ts > %(since)s ORDER BY ts"

    async def go() -> list:
        p = SqlPoller(3, db_url, q, {"dbtm": "ft", "sppa": "psi"}, interval_s=0.1)
        stream = p.stream()
        rows = [await asyncio.wait_for(anext(stream), 5) for _ in range(2)]
        await stream.aclose()
        return rows

    a, b = asyncio.run(go())
    assert a.values == pytest.approx({"bit_md": 304.8, "spp": 137.895}, rel=1e-4)
    assert b.ts > a.ts
    with pytest.raises(ValueError):
        SqlPoller(3, db_url, "SELECT * FROM historian")
