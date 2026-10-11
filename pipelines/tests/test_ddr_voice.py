from datetime import date, datetime

from pipelines.ddr import PRINTED_CODE_IADC, Row, iadc, stitch
from pipelines.voice import LessonOut, Transcript, Word, check, locate


def test_iadc_mapping() -> None:
    assert iadc('Drilled 12-1/4" hole from 2,089 ft to 2,528 ft')[0] == PRINTED_CODE_IADC["DRL"]
    assert iadc("Drilled out shoe + 3 m new hole; performed LOT")[0] == 21
    assert iadc("Waited on cement")[0] == 13
    assert iadc("Lost circulation at 2,634.5 m in Barail; 180 bbl lost.")[2] == "loss"
    assert iadc("Pipe stuck at 3,001.0 m in Kopili.")[2] == "stuck_pipe"


def test_stitch_across_midnight() -> None:
    d1, d2 = date(2020, 1, 1), date(2020, 1, 2)
    loss = "Lost circulation at 2,634.5 m in Barail; 180 bbl lost."
    rows = [
        Row(d1, "00:00", "20:00", 20.0, 'Drilled 8-1/2" hole from 2,500.0 m to 2,634.5 m', None),
        Row(d1, "20:00", "24:00", 4.0, loss, 2634.5),
        Row(d2, "00:00", "06:30", 6.5, loss, 2634.5),
        Row(d2, "06:30", "24:00", 17.5, "Circulated hole clean", None),
    ]
    (e,) = stitch(rows)
    assert e.type == "loss" and e.npt_hr == 10.5 and e.days == 2
    assert e.t_start == datetime(2020, 1, 1, 20) and e.t_end == datetime(2020, 1, 2, 6, 30)


def transcript() -> Transcript:
    text = (
        "This is a synthetic recording . At about 2634 metres in the Barail , we had losses . "
        "Pumped LCM pill and reduced flow rate ."
    )
    words = [Word(i * 0.5, i * 0.5 + 0.4, w, 0.95) for i, w in enumerate(text.split())]
    return Transcript("en", 0.99, text, words, "test")


def test_lesson_rules_and_provenance() -> None:
    t = transcript()
    good = LessonOut(
        formation="Barail",
        hazard="loss",
        depth_top=2634,
        depth_base=None,
        depth_unit="m",
        what_happened="Losses in the Barail.",
        what_worked="Pumped LCM pill and reduced flow rate.",
        evidence="At about 2634 metres in the Barail, we had losses",
    )
    x = check(good, t, {"barail": 4})
    assert x.ok and x.formation_id == 4 and x.md_top == 2634
    assert x.t_start == 3.0 and x.t_end is not None and x.t_end > x.t_start
    assert x.confidence == 0.95
    bad = good.model_copy(update={"formation": "Atlantis", "evidence": "we struck oil"})
    y = check(bad, t, {"barail": 4})
    assert not y.ok
    assert {f.split(":")[0] for f in y.failures} == {"R-FORMATION", "R-EVIDENCE"}


def test_locate_quote() -> None:
    span = locate("reduced flow rate", transcript().words)
    assert span is not None and span[2] == 1.0
