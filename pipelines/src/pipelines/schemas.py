"""Pydantic models per document type, holding values *as printed* (strings).

They are the schema for guided JSON decoding and validate what an LLM returns. Converting to
metres / sg / ISO dates happens afterwards in norm.canon, for the parser and the LLM alike.
`flatten` turns a model into the field names used everywhere else (and in the gold set):
`well_name`, `tops[2].md`, `time_breakdown[0].hours`, `remarks[1]`.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

S = str | None


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class TopRow(_M):
    formation: str
    md: S = None
    tvd: S = None
    tvdss: S = None


class CasingRow(_M):
    od_in: str = Field(description='Casing outside diameter as printed, e.g. 9-5/8"')
    hole_in: S = None
    shoe_md: S = None
    shoe_tvd: S = None
    weight_ppf: S = None
    grade: S = None


class LotRow(_M):
    test_type: str = Field(description="LOT or FIT")
    casing: S = Field(None, description="Casing size the test was below")
    md: S = None
    emw: S = None


class ProblemRow(_M):
    date: S = None
    type: str = Field(description="Problem as printed, e.g. Lost circulation, Stuck pipe, Kick")
    md: S = None
    formation: S = None
    npt_hr: S = None
    volume_bbl: S = None
    mw: S = None


class WCR(_M):
    well_name: str
    block: S = None
    field: S = None
    latitude: S = None
    longitude: S = None
    coordinate_datum: S = None
    depth_reference: S = Field(None, description="KB, RT, DF or GL")
    datum_elevation: S = Field(None, description="Elevation of the depth reference, with unit")
    ground_elevation: S = None
    spud_date: S = None
    completion_date: S = Field(None, description="Rig release or completion date")
    td_md: S = None
    td_tvd: S = None
    trajectory_type: S = None
    depth_unit: Literal["m", "ft"] | None = Field(None, description="Unit of depths in tables")
    tops: list[TopRow] = []
    casing: list[CasingRow] = []
    lot_fit: list[LotRow] = []
    events: list[ProblemRow] = []


class TimeRow(_M):
    from_: str = Field(alias="from", description="Start time HH:MM")
    to: str = Field(description="End time HH:MM (24:00 for midnight)")
    hours: str
    code: S = None
    description: S = None


class DDR(_M):
    well_name: str
    report_no: S = None
    report_date: S = None
    depth_md: S = Field(None, description="Depth at report time, with unit")
    progress: S = None
    mud_weight: S = None
    hole_size: S = None
    total_hours: S = None
    npt_hours: S = None
    depth_unit: Literal["m", "ft"] | None = None
    time_breakdown: list[TimeRow] = []
    remarks: list[str] = []


class SurveyRow(_M):
    md: str
    inc: S = None
    azi: S = None
    tvd: S = None
    north: S = None
    east: S = None


class Survey(_M):
    well_name: str
    depth_unit: Literal["m", "ft"] | None = None
    survey: list[SurveyRow] = []


class FieldNote(_M):
    date: S = None
    well_name: S = None
    event_type: S = Field(None, description="What happened, as written (losses, stuck pipe, kick)")
    depth_md: S = Field(None, description="Depth as written, with unit")
    formation: S = None
    loss_bbl: S = None
    pit_gain_bbl: S = None
    mud_weight: S = None
    mitigation: list[str] | None = None
    npt_hours: S = None
    depth_unit: Literal["m", "ft"] | None = None


SCHEMAS: dict[str, type[_M]] = {
    "wcr": WCR,
    "ddr": DDR,
    "survey_report": Survey,
    "field_note": FieldNote,
}


def flatten(model: _M) -> dict[str, Any]:
    """Model -> {field name: printed string (or list of strings)}; None fields dropped."""
    out: dict[str, Any] = {}
    for name, value in model.model_dump(by_alias=True).items():
        if name == "depth_unit" or value is None:
            continue
        if isinstance(value, list) and value and isinstance(value[0], dict):
            for i, row in enumerate(value):
                for k, v in row.items():
                    if v is not None:
                        out[f"{name}[{i}].{k}"] = v
        elif isinstance(value, list) and name == "remarks":
            for i, v in enumerate(value):
                out[f"remarks[{i}]"] = v
        elif value != []:
            out[name] = value
    return out
