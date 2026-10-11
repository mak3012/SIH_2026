from datetime import date

from pipelines import sodir

WELLS_CSV = (
    "﻿wlbWellboreName,wlbNsDecDeg,wlbEwDecDeg,wlbEntryDate,wlbNpdidWellbore,wlbPurpose,"
    "wlbTotalDepth,wlbMainArea\n"
    "7435/12-1,74.071725,35.808628,09.08.2017,8228,WILDCAT,1540.0,BARENTS SEA\n"
    "1/2-1,,,20.03.1989,1382,WILDCAT,,NORTH SEA\n"  # no location: skipped
    "2/4-X,56.5,3.2,,abc,,,\n"  # no id: skipped
).encode()

DOCS = [
    {"wlbDocumentName": "1382_1_2_1_COMPLETION_REPORT", "wlbNpdidWellbore": "1382",
     "wlbDocumentUrl": "https://x/1382_1_2_1_COMPLETION_REPORT.pdf", "wlbDocumentSize": "155"},
    {"wlbDocumentName": "1382_1_2_1_COMPLETION_LOG", "wlbNpdidWellbore": "1382",
     "wlbDocumentUrl": "https://x/1382_1_2_1_COMPLETION_LOG.pdf", "wlbDocumentSize": "17"},
]  # fmt: skip


def test_wellbores_keep_located_rows_and_parse_dates_and_numbers() -> None:
    rows = sodir.table("wellbore_exploration_all", fetch=lambda url: WELLS_CSV)
    (w,) = sodir.wellbores(rows)
    assert (w.id, w.name, w.lat, w.lon) == (8228, "7435/12-1", 74.071725, 35.808628)
    assert w.entry_date == date(2017, 8, 9) and w.total_depth_m == 1540.0
    assert w.completion_date is None and w.area == "BARENTS SEA"


def test_completion_reports_only_not_logs() -> None:
    assert sodir.completion_reports(DOCS) == {
        1382: [("1382_1_2_1_COMPLETION_REPORT", "https://x/1382_1_2_1_COMPLETION_REPORT.pdf", 155)]
    }
