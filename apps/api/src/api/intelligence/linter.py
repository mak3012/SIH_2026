"""Drilling-program linter and briefing.

RULES check the uploaded program (casing seats, mud-weight schedule, mud type, cement plan)
against the hazard roadmap and offset evidence (LOT/FIT, losses, kicks). Every finding carries
the numbers it used and the evidence it rests on (`E1`, `E2`, ... with well, document, page).

The LLM then drafts a plain-language briefing from the findings, citing evidence ids. It does
not decide anything (ADR-0002). RULES verify the draft: every number in it must match a number
in the findings, the evidence or the roadmap (at the precision written), every paragraph with a
number must cite evidence, and every citation must exist. A draft that fails twice is replaced
by rule-generated text, and the briefing says so.
"""

import io
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from api.intelligence.hazards import Cell
from core.canonical.equipment import CASING_OD, HOLE_SIZES, size_label
from core.telemetry import VERIFIER_REJECTS

RULESET = "lint@1.0.0"
P_BLOCK, P_WARN = 0.30, 0.10  # posterior mean thresholds for "block" / "warn"
OVERLAP_M = 150.0  # cement overlap above the previous shoe
FRAC_MARGIN_SG = 0.03  # MW + ECD margin must stay this far below the weakest shoe test
KICK_MARGIN_SG = 0.05  # MW above the highest kick MW seen in the formation


class Section(BaseModel):
    name: str
    md_top: float = Field(ge=0, description="m MD")
    md_base: float = Field(description="m MD")
    hole_in: float
    casing_od_in: float | None = None
    shoe_md: float | None = None
    mud_type: Literal["wbm", "obm", "sbm", "other"] | None = None
    mw_sg_min: float = Field(ge=0.8, le=2.6)
    mw_sg_max: float = Field(ge=0.8, le=2.6)

    @model_validator(mode="after")
    def order(self) -> "Section":
        if self.md_base <= self.md_top or self.mw_sg_max < self.mw_sg_min:
            raise ValueError(f"{self.name}: md_base > md_top and mw_sg_max >= mw_sg_min required")
        return self


class CementJob(BaseModel):
    casing_od_in: float
    toc_md: float = Field(ge=0)
    slurry_density_sg: float = Field(ge=1.0, le=2.6)


class Program(BaseModel):
    """Units: metres MD, specific gravity, inches for pipe."""

    wellbore_id: int
    td_md: float
    ecd_margin_sg: float = Field(0.05, ge=0, le=0.5, description="expected ECD minus MW")
    sections: list[Section]
    cement: list[CementJob] = []


@dataclass
class Evidence:
    id: str
    kind: str  # lot | fit | loss | kick | stuck_pipe | roadmap
    well: str
    formation: str | None
    md: float | None
    value: float | None
    unit: str | None
    source: dict[str, Any] = field(default_factory=dict)


@dataclass
class Finding:
    rule: str
    severity: str  # block | warn | info
    message: str
    section: str | None = None
    formation: str | None = None
    numbers: dict[str, float] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)


class EvidenceSet:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        """items: offset facts {kind, well, formation, md, value, unit, source}."""
        self.items = [Evidence(f"E{i}", **it) for i, it in enumerate(items, start=1)]

    def of(self, kind: str, formation: str | None = None) -> list[Evidence]:
        return [
            e
            for e in self.items
            if e.kind == kind and (formation is None or e.formation == formation)
        ]


def _formations_in(cells: list[Cell], md_top: float, md_base: float) -> set[str]:
    return {
        c.formation for c in cells if c.band_md and c.band_md[1] > md_top and c.band_md[0] < md_base
    }


def lint(
    p: Program, cells: list[Cell], ev: EvidenceSet, formation_at: dict[str, tuple[float, float]]
) -> list[Finding]:
    """`formation_at`: formation -> (top MD, base MD) along the active well's plan."""
    out: list[Finding] = []
    secs = sorted(p.sections, key=lambda s: s.md_top)

    # Program structure.
    for s in secs:
        if size_label(s.hole_in) not in HOLE_SIZES:
            out.append(
                Finding(
                    "L-STD-HOLE",
                    "warn",
                    f"{s.name}: {size_label(s.hole_in)} in is not a common bit size",
                    s.name,
                    numbers={"hole_in": s.hole_in},
                )
            )
        if s.casing_od_in is not None:
            if size_label(s.casing_od_in) not in CASING_OD:
                out.append(
                    Finding(
                        "L-STD-CASING",
                        "block",
                        f"{s.name}: {size_label(s.casing_od_in)} in is not an API casing size",
                        s.name,
                        numbers={"casing_od_in": s.casing_od_in},
                    )
                )
            if s.hole_in <= s.casing_od_in:
                out.append(
                    Finding(
                        "L-CLEARANCE",
                        "block",
                        f"{s.name}: hole {s.hole_in} in is not"
                        f" larger than casing {s.casing_od_in} in",
                        s.name,
                        numbers={"hole_in": s.hole_in, "casing_od_in": s.casing_od_in},
                    )
                )
    for a, b in zip(secs, secs[1:], strict=False):
        if abs(b.md_top - a.md_base) > 0.5:
            out.append(
                Finding(
                    "L-CONTIGUOUS",
                    "block",
                    f"{b.name} starts at {b.md_top:.0f} m but {a.name} ends at {a.md_base:.0f} m",
                    b.name,
                    numbers={"md_top": b.md_top, "md_base": a.md_base},
                )
            )
        if a.casing_od_in and b.casing_od_in and b.casing_od_in >= a.casing_od_in:
            out.append(
                Finding(
                    "L-CASING-ORDER",
                    "block",
                    f"{b.name}: casing {b.casing_od_in} in does not fit inside {a.casing_od_in} in",
                    b.name,
                )
            )
    if secs and abs(secs[-1].md_base - p.td_md) > 0.5:
        out.append(
            Finding(
                "L-TD",
                "block",
                f"sections end at {secs[-1].md_base:.0f} m, TD is {p.td_md:.0f} m",
                numbers={"md_base": secs[-1].md_base, "td_md": p.td_md},
            )
        )

    shoes = [(s.shoe_md or s.md_base, s) for s in secs if s.casing_od_in]
    for s in secs:
        prev = [(md, x) for md, x in shoes if md <= s.md_top + 0.5]
        prev_shoe = prev[-1] if prev else None
        ecd = s.mw_sg_max + p.ecd_margin_sg
        forms = {f for f, (t, b) in formation_at.items() if b > s.md_top and t < s.md_base}

        # Shoe strength: the weakest offset LOT/FIT in the formation the previous shoe sits in.
        if prev_shoe is not None:
            shoe_form = next(
                (f for f, (t, b) in formation_at.items() if t <= prev_shoe[0] < b), None
            )
            tests = ev.of("lot", shoe_form) + ev.of("fit", shoe_form)
            if tests:
                weakest = min(tests, key=lambda e: e.value or 99)
                if weakest.value is not None and ecd > weakest.value - FRAC_MARGIN_SG:
                    out.append(
                        Finding(
                            "L-MW-FRAC",
                            "block",
                            f"{s.name}: planned ECD {ecd:.2f} sg exceeds the weakest offset "
                            "shoe test "
                            f"in the {shoe_form} ({weakest.value:.2f} sg at {weakest.well}) less "
                            f"{FRAC_MARGIN_SG:.2f} sg",
                            s.name,
                            shoe_form,
                            {
                                "ecd_sg": round(ecd, 2),
                                "shoe_test_sg": weakest.value,
                                "margin_sg": FRAC_MARGIN_SG,
                            },
                            [weakest.id],
                        )
                    )

        for f in sorted(forms):
            loss = next((c for c in cells if c.formation == f and c.hazard == "loss"), None)
            losses = ev.of("loss", f)
            if loss and loss.mean >= P_WARN and losses:
                low = min((e for e in losses if e.value), key=lambda e: e.value or 99, default=None)
                if low is not None and low.value is not None and ecd >= low.value:
                    sev = "block" if loss.mean >= P_BLOCK else "warn"
                    out.append(
                        Finding(
                            "L-SEAT-LOSS",
                            sev,
                            f"{s.name}: ECD {ecd:.2f} sg in the {f} reaches "
                            f"{low.value:.2f} sg, the "
                            f"lowest ECD at which offsets lost returns there (loss probability "
                            f"{loss.mean:.0%}, 90 % interval "
                            f"{loss.ci90[0]:.0%}-{loss.ci90[1]:.0%}). "
                            "Set casing above the loss band or lower the mud weight.",
                            s.name,
                            f,
                            {
                                "ecd_sg": round(ecd, 2),
                                "loss_ecd_sg": low.value,
                                "p_loss": loss.mean,
                                "interval": 0.9,
                                "ci_low": loss.ci90[0],
                                "ci_high": loss.ci90[1],
                            },
                            [low.id] + [e.id for e in losses if e.id != low.id][:3],
                        )
                    )
            kick = next((c for c in cells if c.formation == f and c.hazard == "kick"), None)
            kicks = ev.of("kick", f)
            if kick and kick.mean >= P_WARN and kicks:
                hi = max((e for e in kicks if e.value), key=lambda e: e.value or 0, default=None)
                if (
                    hi is not None
                    and hi.value is not None
                    and s.mw_sg_min < hi.value + KICK_MARGIN_SG
                ):
                    out.append(
                        Finding(
                            "L-MW-KICK",
                            "block" if kick.mean >= P_BLOCK else "warn",
                            f"{s.name}: minimum MW {s.mw_sg_min:.2f} sg in the {f} is below "
                            f"{hi.value + KICK_MARGIN_SG:.2f} sg: offsets took kicks there at "
                            f"{hi.value:.2f} sg (kick probability {kick.mean:.0%}).",
                            s.name,
                            f,
                            {
                                "mw_min_sg": s.mw_sg_min,
                                "kick_mw_sg": hi.value,
                                "required_sg": round(hi.value + KICK_MARGIN_SG, 2),
                                "p_kick": kick.mean,
                            },
                            [hi.id],
                        )
                    )
            stuck = next((c for c in cells if c.formation == f and c.hazard == "stuck_pipe"), None)
            if stuck and stuck.mean >= 0.2 and s.mud_type == "wbm":
                ids = [e.id for e in ev.of("stuck_pipe", f)][:3]
                out.append(
                    Finding(
                        "L-MUD-STUCK",
                        "warn",
                        f"{s.name}: water-based mud through the {f}, where stuck pipe "
                        "probability is "
                        f"{stuck.mean:.0%}. Consider an inhibitive system or OBM/SBM.",
                        s.name,
                        f,
                        {"p_stuck": stuck.mean},
                        ids,
                    )
                )
            gap = next(
                (
                    c
                    for c in cells
                    if c.formation == f
                    and c.counts["unknown"]
                    and c.counts["unknown"] >= c.counts["event"] + c.counts["none"]
                ),
                None,
            )
            if gap is not None:
                out.append(
                    Finding(
                        "L-DATA-GAP",
                        "info",
                        f"{f}: for {gap.counts['unknown']} of the offsets that drilled it, "
                        "problems "
                        "were not documented; its hazard numbers rest on few wells.",
                        s.name,
                        f,
                        {"unknown_wells": gap.counts["unknown"]},
                    )
                )

    # Cement plan.
    for c in p.cement:
        sec = next(
            (s for s in secs if s.casing_od_in and abs(s.casing_od_in - c.casing_od_in) < 1e-3),
            None,
        )
        if sec is None:
            out.append(
                Finding(
                    "L-CMT-CASING",
                    "block",
                    f"cement job for {c.casing_od_in} in casing, which the program does not run",
                )
            )
            continue
        prev_mds = [md for md, _ in shoes if md <= sec.md_top + 0.5]
        if prev_mds and c.toc_md > prev_mds[-1] - OVERLAP_M:
            out.append(
                Finding(
                    "L-CMT-TOC",
                    "warn",
                    f"{sec.name}: top of cement {c.toc_md:.0f} m is less than "
                    f"{OVERLAP_M:.0f} m above the previous shoe ({prev_mds[-1]:.0f} m)",
                    sec.name,
                    numbers={
                        "toc_md": c.toc_md,
                        "prev_shoe_md": prev_mds[-1],
                        "overlap_m": OVERLAP_M,
                    },
                )
            )
        if prev_mds:
            shoe_form = next(
                (f for f, (t, b) in formation_at.items() if t <= prev_mds[-1] < b), None
            )
            tests = ev.of("lot", shoe_form) + ev.of("fit", shoe_form)
            weakest_c = min(tests, key=lambda e: e.value or 99, default=None)
            if (
                weakest_c is not None
                and weakest_c.value is not None
                and c.slurry_density_sg > weakest_c.value - FRAC_MARGIN_SG
            ):
                out.append(
                    Finding(
                        "L-CMT-DENSITY",
                        "block",
                        f"{sec.name}: slurry {c.slurry_density_sg:.2f} sg exceeds the "
                        "weakest offset "
                        f"shoe test in the {shoe_form} ({weakest_c.value:.2f} sg) less "
                        f"{FRAC_MARGIN_SG:.2f} sg: losses while cementing",
                        sec.name,
                        shoe_form,
                        {"slurry_sg": c.slurry_density_sg, "shoe_test_sg": weakest_c.value},
                        [weakest_c.id],
                    )
                )
    order = {"block": 0, "warn": 1, "info": 2}
    return sorted(out, key=lambda f: (order[f.severity], f.rule, f.section or ""))


# ------------------------------------------------------------------ briefing


class Paragraph(BaseModel):
    text: str
    citations: list[str]


class BriefingDraft(BaseModel):
    title: str
    paragraphs: list[Paragraph]


# Unsigned: in "30%-50%" the hyphen is a range, not a minus. Magnitudes are compared.
NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?![\w])")


def program_numbers(findings: list[Finding]) -> list[float]:
    """Numbers of findings that rest on the program alone (no offset evidence to cite)."""
    vals = [abs(float(v)) for f in findings if not f.evidence for v in f.numbers.values()]
    return vals + [v * 100 for v in vals if 0 <= v <= 1]


def allowed_numbers(findings: list[Finding], ev: EvidenceSet, cells: list[Cell]) -> list[float]:
    vals: list[float] = []
    for f in findings:
        vals += [abs(float(v)) for v in f.numbers.values()]
    for e in ev.items:
        vals += [x for x in (e.md, e.value) if x is not None]
    for c in cells:
        vals += [c.mean, *c.ci90, c.n_eff, *(c.band_md or ()), *(c.band_tvdss or ())]
        vals += [float(v) for v in c.counts.values()]
    vals = [abs(v) for v in vals]
    vals += [v * 100 for v in list(vals) if 0 <= v <= 1]  # probabilities written as percent
    return vals


def _matches(token: str, vals: list[float]) -> bool:
    x = float(token.replace(",", ""))
    decimals = len(token.split(".")[1]) if "." in token else 0
    tol = 0.5 * 10 ** (-decimals) + 1e-9
    return any(abs(v - x) <= tol for v in vals)


def verify(
    draft: BriefingDraft,
    findings: list[Finding],
    ev: EvidenceSet,
    cells: list[Cell],
    protected: list[str],
) -> list[str]:
    """Problems with a draft; empty list = verified. `protected` strings (well names, section
    names, casing sizes) are removed before numbers are read."""
    numeric = [float(x) for x in protected if re.fullmatch(r"\d+(\.\d+)?", x)]
    words = [x for x in protected if x and not re.fullmatch(r"\d+(\.\d+)?", x)]
    vals = allowed_numbers(findings, ev, cells) + numeric  # e.g. a "7 in" casing size
    own = program_numbers(findings) + numeric
    ids = {e.id for e in ev.items}
    problems = []
    for i, par in enumerate(draft.paragraphs, start=1):
        text = par.text
        for w in sorted(words, key=len, reverse=True):  # whole words only: "7" is not in "2700"
            text = re.sub(rf"(?<![\w.]){re.escape(w)}(?![\w])", " ", text)
        text = re.sub(r"\[?E\d+\]?", " ", text)
        text = re.sub(r"\d+-\d+/\d+|\d+/\d+", " ", text)  # pipe sizes like 9-5/8
        nums = NUMBER.findall(text)
        for n in nums:
            if not _matches(n, vals):
                problems.append(f"paragraph {i}: number {n} is not in the findings or evidence")
        bad = [c for c in par.citations if c not in ids]
        if bad:
            problems.append(f"paragraph {i}: unknown citation(s) {', '.join(bad)}")
        if not par.citations and any(not _matches(n, own) for n in nums):
            problems.append(f"paragraph {i}: states offset numbers but cites no evidence")
    return problems


def fallback(findings: list[Finding], title: str) -> BriefingDraft:
    """Rule-generated briefing: the findings themselves, one paragraph each."""
    paras = [
        Paragraph(text=f"{f.severity.upper()} ({f.rule}): {f.message}", citations=f.evidence)
        for f in findings
    ] or [Paragraph(text="No rule findings for this program.", citations=[])]
    return BriefingDraft(title=title, paragraphs=paras)


@dataclass
class Briefing:
    draft: BriefingDraft
    status: str  # verified | fallback
    problems: list[list[str]]  # per attempt
    model: str | None

    def json(self) -> dict[str, Any]:
        return {
            "draft": self.draft.model_dump(),
            "status": self.status,
            "problems": self.problems,
            "model": self.model,
        }


def brief(
    llm: Any,
    well: str,
    findings: list[Finding],
    ev: EvidenceSet,
    cells: list[Cell],
    protected: list[str],
    attempts: int = 2,
) -> Briefing:
    title = f"Pre-spud hazard briefing, {well}"
    history: list[list[str]] = []
    if llm is not None and getattr(llm, "enabled", False):
        feedback = ""
        for _ in range(attempts):
            out = llm.complete_json(
                "briefing",
                {
                    "well": well,
                    "findings": _findings_text(findings),
                    "evidence": _evidence_text(ev),
                    "roadmap": _roadmap_text(cells),
                    "feedback": feedback,
                },
                BriefingDraft.model_json_schema(),
                validate=BriefingDraft.model_validate,
            )
            if out is None:
                history.append(["no valid JSON from the model"])
                break
            problems = verify(out, findings, ev, cells, protected)
            history.append(problems)
            if problems:
                VERIFIER_REJECTS.labels("briefing").inc()
            else:
                return Briefing(out, "verified", history, llm.cfg.model)
            feedback = (
                "Your previous draft failed verification:\n- "
                + "\n- ".join(problems)
                + "\nUse only numbers that appear in the findings, evidence or roadmap, "
                "exactly as given, and cite evidence ids for every number."
            )
    draft = fallback(findings, title)
    history.append(verify(draft, findings, ev, cells, protected))
    return Briefing(draft, "fallback", history, None)


def _findings_text(fs: list[Finding]) -> str:
    return (
        "\n".join(
            f"- [{f.severity}] {f.rule}: {f.message} numbers={f.numbers} evidence={f.evidence}"
            for f in fs
        )
        or "(none)"
    )


def _evidence_text(ev: EvidenceSet) -> str:
    return (
        "\n".join(
            f"- {e.id}: {e.kind} at {e.well}, {e.formation or '-'}, MD {e.md} m, "
            f"value {e.value} {e.unit or ''} ({e.source.get('document', '')} p."
            f"{e.source.get('page', '')})"
            for e in ev.items
        )
        or "(none)"
    )


def _roadmap_text(cells: list[Cell]) -> str:
    top = sorted((c for c in cells if c.evidence_state == "evidence"), key=lambda c: -c.mean)[:8]
    return (
        "\n".join(
            f"- {c.formation} {c.hazard}: mean {c.mean}, 90% {c.ci90}, n_eff {c.n_eff},"
            f" counts {c.counts}, band MD {c.band_md}"
            for c in top
        )
        or "(none)"
    )


# ------------------------------------------------------------------ PDF


def pdf(b: Briefing, findings: list[Finding], ev: EvidenceSet, generated_by: str) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph as P
    from reportlab.platypus import SimpleDocTemplate, Spacer, Table, TableStyle

    styles = getSampleStyleSheet()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        title=b.draft.title,
        author=generated_by,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
    )
    status = (
        "Verified: every number and citation in the text was checked against the rule "
        "findings and offset evidence."
        if b.status == "verified"
        else "Rule-generated text: the language-model draft was unavailable or failed verification."
    )
    story = [
        P(_esc(b.draft.title), styles["Title"]),
        P(_esc(status), styles["Italic"]),
        Spacer(1, 4 * mm),
    ]
    for par in b.draft.paragraphs:
        cite = f" [{', '.join(par.citations)}]" if par.citations else ""
        story.append(P(_esc(par.text + cite), styles["BodyText"]))
    story += [Spacer(1, 6 * mm), P("Findings", styles["Heading2"])]
    rows = [["Severity", "Rule", "Finding"]] + [
        [f.severity, f.rule, P(_esc(f.message), styles["BodyText"])] for f in findings
    ]
    t = Table(rows, colWidths=[18 * mm, 28 * mm, 128 * mm], repeatRows=1)
    t.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story += [t, Spacer(1, 6 * mm), P("Evidence", styles["Heading2"])]
    rows = [["Id", "Kind", "Well", "Formation", "MD (m)", "Value", "Source"]] + [
        [
            e.id,
            e.kind,
            e.well,
            e.formation or "",
            "" if e.md is None else f"{e.md:.0f}",
            "" if e.value is None else f"{e.value:g} {e.unit or ''}",
            P(
                _esc(f"{e.source.get('document', '')} p.{e.source.get('page', '')}"),
                styles["BodyText"],
            ),
        ]
        for e in ev.items
    ]
    t = Table(
        rows,
        colWidths=[10 * mm, 16 * mm, 20 * mm, 22 * mm, 16 * mm, 22 * mm, 68 * mm],
        repeatRows=1,
    )
    t.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story += [
        t,
        Spacer(1, 4 * mm),
        P(
            _esc(
                f"Generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC by {generated_by}; "
                f"ruleset {RULESET}; "
                f"model {b.model or 'none'}. Decisions are the rules'; the text only explains them."
            ),
            styles["Italic"],
        ),
    ]
    doc.build(story)
    return buf.getvalue()


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def findings_json(fs: list[Finding]) -> list[dict[str, Any]]:
    return [asdict(f) for f in fs]
