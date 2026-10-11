"""Connector adapters. Each yields canonical `Sample`s as an async iterator; reconnecting,
heartbeat and staleness are the service's job (service.py).

- WitsmlPoller  WITSML 1.4.1.1 store (SOAP WMLS_GetFromStore), time-indexed log, polled for rows
                newer than the last one received.
- Etp12Client   ETP 1.2 (WebSocket, Avro) - interface stub only.
- Wits0Listener WITS level 0 over TCP: "&&" ... "!!" frames of 4-digit item + value lines.
- OpcUaClient   OPC-UA nodes read every interval (asyncua), with source timestamps.
- SqlPoller     A historian table/view polled with a query taking %(since)s.

All feeds are read-only: no connector writes to the rig side (one-way OT boundary).
Credentials come from the environment, never from rt_feed.config.
"""

import asyncio
import base64
import logging
import os
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Protocol
from xml.sax.saxutils import escape

from api.realtime.channels import CHANNELS, WITS0_ITEMS, WITSML_MNEMONICS, Sample, factor

log = logging.getLogger(__name__)
WML = "http://www.witsml.org/schemas/1series"
SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
WMLS = "http://www.witsml.org/message/120"


class Connector(Protocol):
    kind: str

    def stream(self) -> AsyncIterator[Sample]: ...


def parse_time(s: str) -> datetime:
    t = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def canonical(name: str, unit: str | None) -> tuple[str, float] | None:
    """(channel, factor) for a mnemonic or canonical name; None when unmapped or the unit
    cannot convert (logged once per call site)."""
    ch = name if name in CHANNELS else WITSML_MNEMONICS.get(name.strip().upper())
    if ch is None:
        return None
    try:
        return ch, factor(unit, ch)
    except ValueError as e:
        log.warning("skipping %s: %s", name, e)
        return None


# ------------------------------------------------------------------ WITSML 1.4.1.1


def parse_logs(xml: str) -> list[tuple[datetime, dict[str, float]]]:
    """Rows of time-indexed WITSML 1.4.1.1 <logs>: (time, canonical values). Shared by the
    store poller, the simulator's Volve source and tests."""
    root = ET.fromstring(xml)
    out: list[tuple[datetime, dict[str, float]]] = []
    for lg in root.iter(f"{{{WML}}}log"):
        if "depth" in (lg.findtext(f"{{{WML}}}indexType") or "date time"):
            continue  # depth-indexed logs are not a time series
        index = (lg.findtext(f"{{{WML}}}indexCurve") or "TIME").strip()
        for ld in lg.iter(f"{{{WML}}}logData"):
            mnems = (ld.findtext(f"{{{WML}}}mnemonicList") or "").split(",")
            units = (ld.findtext(f"{{{WML}}}unitList") or "").split(",")
            units += [""] * (len(mnems) - len(units))
            if index not in mnems:
                raise ValueError(f"index curve {index} not in mnemonicList")
            i_t = mnems.index(index)
            maps = [
                None if i == i_t else canonical(m, u)
                for i, (m, u) in enumerate(zip(mnems, units, strict=True))
            ]
            for d in ld.iter(f"{{{WML}}}data"):
                cells = (d.text or "").split(",")
                vals: dict[str, float] = {}
                for cell, mp in zip(cells, maps, strict=False):
                    if mp is not None and cell.strip() not in ("", "-999.25", "NaN"):
                        vals[mp[0]] = float(cell) * mp[1]
                out.append((parse_time(cells[i_t]), vals))
    out.sort(key=lambda r: r[0])
    return out


def soap_get_from_store(query: str) -> bytes:
    return (
        f'<soap:Envelope xmlns:soap="{SOAP}" xmlns:wml="{WMLS}"><soap:Body>'
        "<wml:WMLS_GetFromStore><WMLtypeIn>log</WMLtypeIn>"
        f"<QueryIn>{escape(query)}</QueryIn><OptionsIn>returnElements=data-only</OptionsIn>"
        "<CapabilitiesIn></CapabilitiesIn></wml:WMLS_GetFromStore></soap:Body></soap:Envelope>"
    ).encode()


def log_query(uid_well: str, uid_wellbore: str, uid_log: str, since: datetime | None) -> str:
    start = f"<startDateTimeIndex>{since.isoformat()}</startDateTimeIndex>" if since else ""
    return (
        f'<logs xmlns="{WML}" version="1.4.1.1"><log uidWell="{escape(uid_well)}"'
        f' uidWellbore="{escape(uid_wellbore)}" uid="{escape(uid_log)}">{start}'
        "<logData/></log></logs>"
    )


class WitsmlPoller:
    kind = "witsml"

    def __init__(
        self,
        wellbore_id: int,
        url: str,
        uid_well: str,
        uid_wellbore: str,
        uid_log: str,
        interval_s: float = 2.0,
        since: datetime | None = None,
    ) -> None:
        self.wellbore_id, self.url = wellbore_id, url
        self.uids = (uid_well, uid_wellbore, uid_log)
        self.interval_s, self.last = interval_s, since

    def _post(self, body: bytes) -> str:
        req = urllib.request.Request(
            self.url,
            body,
            {
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": f"{WMLS}/action/WMLS_GetFromStore",
            },
        )
        user, pw = os.environ.get("WITSML_USER"), os.environ.get("WITSML_PASSWORD")
        if user:
            req.add_header(
                "Authorization", "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
            )
        with urllib.request.urlopen(req, timeout=30) as r:
            env = ET.fromstring(r.read())
        result = env.findtext(".//Result") or env.findtext(f".//{{{WMLS}}}Result")
        if result is None or int(result) < 0:
            msg = env.findtext(".//SuppMsgOut") or "no Result"
            raise RuntimeError(f"WMLS_GetFromStore failed ({result}): {msg}")
        return env.findtext(".//XMLout") or env.findtext(f".//{{{WMLS}}}XMLout") or ""

    async def stream(self) -> AsyncIterator[Sample]:
        while True:
            xml = await asyncio.to_thread(
                self._post, soap_get_from_store(log_query(*self.uids, self.last))
            )
            for ts, vals in parse_logs(xml) if xml else []:
                if self.last is None or ts > self.last:  # startIndex is inclusive
                    self.last = ts
                    yield Sample(self.wellbore_id, ts, vals)
            await asyncio.sleep(self.interval_s)


# ------------------------------------------------------------------ ETP 1.2 (stub)


class Etp12Client:
    """ETP 1.2 interface stub. A full client would:

    1. open a WebSocket with subprotocol `etp12.energistics.org` and authenticate (bearer);
    2. RequestSession / OpenSession (Core protocol 0), negotiating ChannelStreaming (1);
    3. ChannelMetadata for the wellbore's channels (URIs eml:///witsml20.Channel(...)), mapping
       mnemonics and units with `canonical()`;
    4. StartStreaming, then decode ChannelData (Avro binary) into Samples.

    Not implemented: no ETP 1.2 server is available to test against. Use WitsmlPoller.
    """

    kind = "etp"

    def __init__(self, wellbore_id: int, url: str, channel_uris: list[str]) -> None:
        self.wellbore_id, self.url, self.channel_uris = wellbore_id, url, channel_uris

    def stream(self) -> AsyncIterator[Sample]:
        raise NotImplementedError("ETP 1.2 is a stub interface; use the WITSML poller")


# ------------------------------------------------------------------ WITS level 0


def parse_wits0(frame: str, units: dict[str, str]) -> tuple[datetime | None, dict[str, float]]:
    """One "&&"...'!!' frame -> (time from items 0105/0106 if sent, canonical values).
    WITS0 carries no units: `units` (channel -> unit) states what the rig sends."""
    vals: dict[str, float] = {}
    date = time = None
    for line in frame.splitlines():
        line = line.strip()
        if len(line) < 5 or not line[:4].isdigit():
            continue
        item, value = line[:4], line[4:].strip()
        if item == "0105":
            date = value
        elif item == "0106":
            time = value
        elif (ch := WITS0_ITEMS.get(item)) is not None:
            try:
                vals[ch] = float(value) * factor(units.get(ch, CHANNELS[ch][0]), ch)
            except ValueError:
                log.warning("WITS0 item %s: bad value %r", item, value)
    ts = None
    if date and time and len(date) == 6 and len(time) == 6:
        ts = datetime.strptime(date + time, "%y%m%d%H%M%S").replace(tzinfo=UTC)
    return ts, vals


METRIC = {
    "bit_md": "m",
    "hole_md": "m",
    "block_pos": "m",
    "hookload": "kN",
    "wob": "kN",
    "torque": "kN.m",
    "spp": "kPa",
    "flow_in": "L/min",
    "flow_out": "L/min",
    "pit_vol": "m3",
    "mw_in": "kg/m3",
    "rop": "m/h",
}


class Wits0Listener:
    """TCP server; the rig's WITS0 serial-to-IP converter connects to it."""

    kind = "wits0"

    def __init__(
        self,
        wellbore_id: int,
        host: str = "0.0.0.0",
        port: int = 7000,
        units: dict[str, str] | None = None,
    ) -> None:
        self.wellbore_id, self.host, self.port = wellbore_id, host, port
        self.units = units or METRIC
        self.queue: asyncio.Queue[Sample] = asyncio.Queue(maxsize=10_000)
        self.server: asyncio.Server | None = None

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        buf = ""
        try:
            while data := await reader.read(4096):
                buf += data.decode("ascii", "replace")
                while "&&" in buf and "!!" in buf[buf.index("&&") :]:
                    start = buf.index("&&")
                    end = buf.index("!!", start)
                    ts, vals = parse_wits0(buf[start + 2 : end], self.units)
                    buf = buf[end + 2 :]
                    if vals:
                        await self.queue.put(
                            Sample(self.wellbore_id, ts or datetime.now(UTC), vals)
                        )
        finally:
            writer.close()

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._client, self.host, self.port)
        return int(self.server.sockets[0].getsockname()[1])

    async def stream(self) -> AsyncIterator[Sample]:
        if self.server is None:
            await self.start()
        while True:
            yield await self.queue.get()


# ------------------------------------------------------------------ OPC-UA


class OpcUaClient:
    kind = "opcua"

    def __init__(
        self,
        wellbore_id: int,
        url: str,
        nodes: dict[str, str],
        units: dict[str, str] | None = None,
        interval_s: float = 1.0,
    ) -> None:
        """`nodes`: channel -> node id (e.g. "ns=2;s=Rig1.Hookload"); `units`: channel -> unit."""
        self.wellbore_id, self.url, self.nodes = wellbore_id, url, nodes
        self.units, self.interval_s = units or {}, interval_s

    async def stream(self) -> AsyncIterator[Sample]:
        from asyncua.client.client import Client

        client = Client(self.url)
        user = os.environ.get("OPCUA_USER")
        if user:
            client.set_user(user)
            client.set_password(os.environ.get("OPCUA_PASSWORD", ""))
        async with client:
            handles = {ch: client.get_node(nid) for ch, nid in self.nodes.items()}
            last: datetime | None = None
            while True:
                vals: dict[str, float] = {}
                stamps = []
                for ch, node in handles.items():
                    dv = await node.read_data_value()
                    if dv.Value is not None and dv.Value.Value is not None:
                        vals[ch] = float(dv.Value.Value) * factor(self.units.get(ch), ch)
                        if dv.SourceTimestamp:
                            stamps.append(dv.SourceTimestamp)
                ts = max(stamps) if stamps else datetime.now(UTC)
                ts = ts if ts.tzinfo else ts.replace(tzinfo=UTC)
                if vals and (last is None or ts > last):
                    last = ts
                    yield Sample(self.wellbore_id, ts, vals)
                await asyncio.sleep(self.interval_s)


# ------------------------------------------------------------------ SQL poll


class SqlPoller:
    """Polls a PostgreSQL historian. `query` must return a `ts` column plus channel columns
    (canonical names or WITSML mnemonics) for rows after %(since)s, ordered by ts."""

    kind = "sql"

    def __init__(
        self,
        wellbore_id: int,
        dsn: str,
        query: str,
        units: dict[str, str] | None = None,
        interval_s: float = 5.0,
        since: datetime | None = None,
    ) -> None:
        if "%(since)s" not in query:
            raise ValueError("query must filter on %(since)s")
        self.wellbore_id, self.dsn, self.query = wellbore_id, dsn, query
        self.units, self.interval_s = units or {}, interval_s
        self.since = since or datetime(1970, 1, 1, tzinfo=UTC)

    async def stream(self) -> AsyncIterator[Sample]:
        from psycopg import AsyncConnection

        async with await AsyncConnection.connect(self.dsn, autocommit=True) as conn:
            await conn.execute("SET default_transaction_read_only = on")
            while True:
                cur = await conn.execute(self.query, {"since": self.since})
                cols = [d.name for d in cur.description or []]
                maps: list[Any] = [
                    None if c == "ts" else canonical(c, self.units.get(c)) for c in cols
                ]
                for r in await cur.fetchall():
                    rec = dict(zip(cols, r, strict=True))
                    vals = {
                        mp[0]: float(v) * mp[1]
                        for v, mp in zip(r, maps, strict=True)
                        if mp is not None and v is not None
                    }
                    ts = rec["ts"] if rec["ts"].tzinfo else rec["ts"].replace(tzinfo=UTC)
                    self.since = max(self.since, ts)
                    yield Sample(self.wellbore_id, ts, vals)
                await asyncio.sleep(self.interval_s)
