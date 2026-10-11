"""Pass A: layout-aware template extraction (labels and table headers, not coordinates).

Finds values by their labels ("Spud date", "Depth @ 24:00") and tables by their header rows
("Formation | MD | TVD"), then reads cells in the columns the headers define. It works on any
engine's cells (text layer, Docling, PaddleOCR, VLM), so it also serves as the second pass on
an alternative layout. Label and header synonyms live in the tables below; adding a document
type means adding a parser here and a schema in schemas.py.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from core.canonical.units import ConversionLog
from pipelines.layout import Cell, PageLayout
from pipelines.norm import DEPTH_FIELDS, FLOAT_FIELDS, SG_FIELDS, canon, unit_of

Pages = list[PageLayout]
PROBLEMS_SECTION = "drilling_problems_section"


@dataclass
class Value:
    name: str
    value: Any
    printed: str
    page: int
    bbox: list[float]
    ocr_conf: float
    source: str = "parser"
    error: str | None = None  # printed text found but not readable
    conversions: list[dict[str, Any]] = field(default_factory=list)  # unit conversions applied


@dataclass
class Extraction:
    doc_type: str
    depth_unit: str | None
    values: dict[str, Value] = field(default_factory=dict)
    source: str = "parser"
    notes: list[str] = field(default_factory=list)

    def add(
        self,
        name: str,
        printed: Any,
        page: int,
        bbox: list[float],
        conf: float,
        unit: str | None = None,
    ) -> None:
        log = ConversionLog()
        try:
            value, err = canon(name, printed, unit or self.depth_unit, log), None
        except ValueError as e:
            value, err = None, str(e)
        text = "; ".join(printed) if isinstance(printed, list) else str(printed)
        self.values[name] = Value(
            name, value, text, page, bbox, conf, self.source, err, log.as_list()
        )


def _n(t: str) -> str:
    return re.sub(r"\s+", " ", t.strip().lower())


def kv(pages: Pages, labels: list[str]) -> tuple[str, int, list[float], float] | None:
    """Value printed next to (or after) a label: (printed, page, bbox, conf).

    Strict matches anywhere in the document win over relaxed ones: a cell that is the label
    ("Well name" | "SA-01") or starts "Label:" first; only then a label merged with its value
    in one OCR cell ("Block SA-BLK-2", "... Progress:1,898 ft MW:")."""
    rows = [(p, row) for p in pages for row in p.rows()]
    for relaxed in (False, True):
        for lab in labels:
            lab_n = _n(lab)
            for p, row in rows:
                for i, c in enumerate(row):
                    hit = _kv_cell(c, row, i, lab_n, relaxed)
                    if hit is not None:
                        v, text = hit
                        return text, p.page, v.bbox, v.conf
    return None


def _kv_cell(c: Cell, row: list[Cell], i: int, lab_n: str, relaxed: bool
             ) -> tuple[Cell, str] | None:  # fmt: skip
    t = _n(c.text)
    if not relaxed:
        if t in (lab_n, lab_n + ":") and i + 1 < len(row):
            return row[i + 1], row[i + 1].text
        if t.startswith(lab_n + ":"):
            rest = _until_next_label(c.text.strip()[len(lab_n) :].lstrip(" :"))
            return (c, rest) if rest else None
        return None
    rest_raw = c.text.strip()[len(lab_n) :].strip()
    # "Block SA-BLK-2": the value starts with a capital or digit and the rest of the cell is
    # not itself another label; "WELL COMPLETION REPORT" is a title, not the label "Well".
    if t.startswith(lab_n + " ") and not c.text.strip().isupper():
        if " " in lab_n or rest_raw[:1].isupper() or rest_raw[:1].isdigit():
            rest = _until_next_label(rest_raw)
            if rest:
                return c, rest
    m = re.search(rf"(?:^|\s){re.escape(lab_n)}\s*:\s*", t)
    if m and m.start() > 0:
        rest = _until_next_label(c.text.strip()[m.end() :])
        if rest:
            return c, rest
        if m.end() >= len(t) and i + 1 < len(row):  # "... MW:" | "1.19 sg"
            return row[i + 1], row[i + 1].text
    return None


def _until_next_label(s: str) -> str:
    """'3,987 ft Progress:1,898 ft MW:' -> '3,987 ft'."""
    m = re.search(r"\s[A-Z][A-Za-z @/()]*\s?:", s)
    return (s[: m.start()] if m else s).strip()


TableSpec = dict[str, list[str]]


def _header(row: list[Cell], spec: TableSpec) -> dict[str, Cell]:
    found = {}
    for c in row:
        t = re.sub(r"\(.*?\)", "", _n(c.text)).strip(" :")
        for key, names in spec.items():
            if key not in found and t in names:
                found[key] = c
                break
    return found if len(found) >= 2 else {}


def table(
    pages: Pages, spec: TableSpec, required: set[str], row_ok: Callable[[dict[str, Cell]], bool]
) -> tuple[list[tuple[int, dict[str, Cell]]], str | None]:
    """Rows of every table whose header matches `spec` (continuing across pages).
    Returns [(page, {column: cell})] and the unit named in the header, if any."""
    out: list[tuple[int, dict[str, Cell]]] = []
    unit = None
    for p in pages:
        rows = p.rows()
        i = 0
        while i < len(rows):
            hdr = _header(rows[i], spec)
            if not required <= hdr.keys():
                i += 1
                continue
            unit = unit or next((u for c in hdr.values() if (u := _unit(c.text))), None)
            cols = sorted(hdr.items(), key=lambda kv: kv[1].bbox[0])
            last_y, spacing = rows[i][0].yc, None
            i += 1
            while i < len(rows):
                r = rows[i]
                gap = r[0].yc - last_y
                if (spacing and gap > spacing * 2.6) or r[0].bbox[0] < cols[0][1].bbox[0] - 0.02:
                    break
                rec: dict[str, Cell] = {}
                for c in _split_spanning(r, cols):
                    key = min(cols, key=lambda kv: abs(kv[1].bbox[0] - c.bbox[0]))[0]
                    if key in rec:  # two cells in one column: one value split by a gap
                        a = rec[key]
                        rec[key] = Cell(
                            f"{a.text} {c.text}",
                            [
                                a.bbox[0],
                                min(a.bbox[1], c.bbox[1]),
                                c.bbox[2],
                                max(a.bbox[3], c.bbox[3]),
                            ],
                            min(a.conf, c.conf),
                        )
                    else:
                        rec[key] = c
                if not row_ok(rec):
                    break
                out.append((p.page, rec))
                spacing = spacing or gap
                last_y = r[0].yc
                i += 1
    return out, unit


def _split_spanning(row: list[Cell], cols: list[tuple[str, Cell]]) -> list[Cell]:
    """OCR sometimes returns neighbouring table cells as one ("68 K55"). A cell whose extent
    covers several column starts and has one token per column is split across them."""
    out = []
    starts = [c.bbox[0] for _, c in cols]
    for c in row:
        inside = [x for x in starts if c.bbox[0] - 0.01 <= x < c.bbox[2]]
        tokens = c.text.split()
        if len(inside) > 1 and len(tokens) == len(inside):
            edges = [*inside[1:], c.bbox[2]]
            x0 = c.bbox[0]
            for tok, x1 in zip(tokens, edges, strict=True):
                out.append(Cell(tok, [x0, c.bbox[1], x1, c.bbox[3]], c.conf))
                x0 = x1
        else:
            out.append(c)
    return out


NUMERIC = DEPTH_FIELDS | SG_FIELDS | FLOAT_FIELDS
# A table number: optional sign, digits with separators, optional unit. Stamps and dates fail.
NUMBER_CELL = re.compile(
    r"^[-+~]?(\d{1,3}(,\d{3})+|\d+)?(\.\d+)?\s*(ft|m|sg|h|hrs?|bbl|°|in|\"|%)?$"
)


def _unit(header: str) -> str | None:
    m = re.search(r"\((ft|m)\)", header.lower())
    return m.group(1) if m else None


def _num(c: Cell | None) -> bool:
    return c is not None and bool(re.search(r"\d", c.text))


def _put(
    x: Extraction,
    name: str,
    hit: tuple[str, int, list[float], float] | None,
    unit: str | None = None,
) -> None:
    if hit is not None:
        x.add(name, hit[0], hit[1], hit[2], hit[3], unit)


def _rows(
    x: Extraction, prefix: str, rows: list[tuple[int, dict[str, Cell]]], unit: str | None
) -> None:
    for i, (page, rec) in enumerate(rows):
        for key, c in rec.items():
            name = f"{prefix}[{i}].{key}"
            x.add(name, c.text, page, c.bbox, c.conf, unit)
            if key in NUMERIC and not NUMBER_CELL.match(c.text.strip()):
                v = x.values[name]
                v.value, v.error = None, f"not a number: {c.text!r}"


def parse_wcr(pages: Pages) -> Extraction:
    all_text = "\n".join(p.text() for p in pages)
    m = re.search(r"all depths in (ft|m)\b", all_text, re.I)
    td = kv(pages, ["Total depth (MD)", "TD (MD)", "TD MD", "Total depth"])
    unit = m.group(1).lower() if m else (unit_of(td[0], None) if td else None)
    x = Extraction("wcr", unit)
    for name, labels in {
        "well_name": ["Well name", "Well"],
        "block": ["Block", "Licence", "License"],
        "field": ["Field"],
        "latitude": ["Latitude", "Lat"],
        "longitude": ["Longitude", "Long"],
        "coordinate_datum": ["Coordinate datum", "Geodetic datum", "Datum"],
        "depth_reference": ["Depth reference", "Depth ref", "Datum reference"],
        "datum_elevation": ["KB elevation", "RT elevation", "DF elevation", "Elevation"],
        "ground_elevation": ["Ground level", "Ground elevation", "GL"],
        "spud_date": ["Spud date", "Spudded"],
        "completion_date": ["Rig release", "Completion date", "Completed"],
        "td_md": ["Total depth (MD)", "TD (MD)", "TD MD"],
        "td_tvd": ["Total depth (TVD)", "TD (TVD)", "TD TVD"],
        "trajectory_type": ["Well profile", "Profile", "Well type"],
    }.items():
        _put(x, name, kv(pages, labels))

    tops, u = table(
        pages,
        {
            "formation": ["formation"],
            "md": ["md", "top md"],
            "tvd": ["tvd", "top tvd"],
            "tvdss": ["tvdss", "top tvdss"],
        },
        {"formation", "md", "tvd"},
        lambda r: "formation" in r and _num(r.get("md")),
    )
    _rows(x, "tops", tops, u or unit)
    casing, u = table(
        pages,
        {
            "od_in": ["casing", "csg", "casing od", "size"],
            "hole_in": ["hole", "hole size", "bit"],
            "shoe_md": ["shoe md", "setting depth", "shoe depth"],
            "shoe_tvd": ["shoe tvd"],
            "weight_ppf": ["wt", "weight"],
            "grade": ["grade"],
        },
        {"od_in", "shoe_md"},
        lambda r: _num(r.get("od_in")) and _num(r.get("shoe_md")),
    )
    _rows(x, "casing", casing, u or unit)
    lots, u = table(
        pages,
        {
            "test_type": ["test", "test type"],
            "casing": ["below shoe", "shoe"],
            "md": ["depth md", "md", "depth"],
            "emw": ["emw", "emw (sg)"],
        },
        {"test_type", "emw"},
        lambda r: _num(r.get("emw")),
    )
    _rows(x, "lot_fit", lots, u or unit)
    events, u = table(
        pages,
        {
            "date": ["date"],
            "type": ["problem", "event", "type"],
            "md": ["md", "depth"],
            "formation": ["formation"],
            "npt_hr": ["npt", "npt hrs"],
            "volume_bbl": ["vol", "volume"],
            "mw": ["mw", "mud wt"],
        },
        {"date", "type"},
        lambda r: "type" in r and _num(r.get("md")),
    )
    _rows(x, "events", events, u or unit)
    # A problems section, even one that says there were none, documents the whole well: an event
    # type absent from it is "none", not "unknown" (see event_documentation).
    if events or re.search(
        r"drilling problems|no significant (drilling )?problems", all_text, re.I
    ):
        x.notes.append(PROBLEMS_SECTION)
    return x


def parse_ddr(pages: Pages) -> Extraction:
    depth = kv(
        pages,
        [
            "Depth @ 24:00",
            "Depth@ 24:00",
            "Depth @24:00",
            "Depth at 24:00",
            "Depth",
            "Midnight depth",
        ],
    )
    x = Extraction("ddr", unit_of(depth[0], None) if depth else None)
    for name, labels in {
        "well_name": ["Well name", "Well"],
        "report_no": ["Report no", "Report number", "DDR no"],
        "report_date": ["Date", "Report date"],
        "progress": ["Progress", "Footage"],
        "mud_weight": ["MW", "Mud weight"],
        "hole_size": ["Hole size", "Bit size"],
        "total_hours": ["Total hours", "Total hrs"],
        "npt_hours": ["NPT today", "NPT", "Downtime"],
    }.items():
        _put(x, name, kv(pages, labels))
    _put(x, "depth_md", depth)
    rows, _ = table(
        pages,
        {
            "from": ["from"],
            "to": ["to"],
            "hours": ["hrs", "hours", "hr"],
            "code": ["code"],
            "description": ["operation", "description", "details"],
        },
        {"from", "to", "hours"},
        lambda r: bool(r.get("from") and re.fullmatch(r"\d{1,2}[:.]\d{2}", r["from"].text.strip())),
    )
    _rows(x, "time_breakdown", rows, x.depth_unit)
    k = 0
    for p in pages:
        seen = False
        for row in p.rows():
            text = " ".join(c.text for c in row)
            if _n(text).startswith("remarks"):
                seen = True
                continue
            if seen and not re.match(r"operations normal|page \d+$", _n(text)):
                c = row[0]
                x.add(f"remarks[{k}]", text, p.page, c.bbox, min(r.conf for r in row))
                k += 1
    return x


def parse_survey(pages: Pages) -> Extraction:
    head = next((c for p in pages for c in p.cells if re.match(r"well\s+\S+", c.text, re.I)), None)
    all_text = "\n".join(p.text() for p in pages)
    m = re.search(r"units?\s+(ft|m)\b", all_text, re.I)
    x = Extraction("survey_report", m.group(1).lower() if m else None)
    if head is not None:
        page = next(p.page for p in pages if head in p.cells)
        wm = re.match(r"well\s+(\S+)", head.text, re.I)
        assert wm is not None
        x.add("well_name", wm.group(1), page, head.bbox, head.conf)
    rows, u = table(
        pages,
        {
            "md": ["md"],
            "inc": ["inc", "incl"],
            "azi": ["azi", "azimuth"],
            "tvd": ["tvd"],
            "north": ["n/s", "north", "ns"],
            "east": ["e/w", "east", "ew"],
        },
        {"md", "inc", "azi"},
        lambda r: _num(r.get("md")) and _num(r.get("inc")),
    )
    if x.depth_unit is None:
        x.depth_unit = u
    _rows(x, "survey", rows, u or x.depth_unit)
    return x


FIELD_NOTE_PATTERNS: list[tuple[str, str]] = [
    ("date", r"^\d{1,4}[./-]\w{1,3}[./-]\d{2,4}$"),
    ("well_name", r"^well\b"),
    ("event_type", r"^(losses|loss|stuck|kick|pit gain|well control)"),
    ("depth_md", r"^@|^at\s+\d"),
    ("formation", r"^in\s+[a-z]"),
    ("loss_bbl", r"^lost\b"),
    ("pit_gain_bbl", r"^pit gain"),
    ("mud_weight", r"^mw\b"),
    ("mitigation", r"^action"),
    ("npt_hours", r"^npt\b"),
]


def parse_field_note(pages: Pages) -> Extraction:
    x = Extraction("field_note", None)
    rows = [(p, row) for p in pages for row in p.rows()]
    for _, row in rows:
        text = " ".join(c.text for c in row).strip()
        u = unit_of(text, None)
        if u and re.match(r"^@|^at\s+\d", text, re.I):
            x.depth_unit = u
    for p, row in rows:
        text = " ".join(c.text for c in row).strip()
        for name, pat in FIELD_NOTE_PATTERNS:
            if name not in x.values and re.search(pat, text, re.I):
                box = [
                    min(c.bbox[0] for c in row),
                    min(c.bbox[1] for c in row),
                    max(c.bbox[2] for c in row),
                    max(c.bbox[3] for c in row),
                ]
                conf = min(c.conf for c in row)
                if name == "mud_weight":
                    text = text.split("ECD")[0]
                x.add(name, text, p.page, box, conf)
                break
    return x


PARSERS: dict[str, Callable[[Pages], Extraction]] = {
    "wcr": parse_wcr,
    "ddr": parse_ddr,
    "survey_report": parse_survey,
    "field_note": parse_field_note,
}
