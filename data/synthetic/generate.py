"""Generate a deterministic synthetic well: survey (MD/INC/AZI -> TVD) and 1 Hz drilling channels.

Writes CSVs to data/raw/synthetic/. Usage: uv run python data/synthetic/generate.py [--seed N]
"""

import argparse
import csv
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.depth import Station, minimum_curvature, tvdss

OUT = Path(__file__).resolve().parents[1] / "raw" / "synthetic"
RT_ELEVATION_M = 32.0  # RT above MSL


def survey(rng: random.Random) -> list[Station]:
    # Vertical to 800 m, build 3 deg/30 m to ~45 deg, then hold to 3000 m MD.
    stations, inc = [], 0.0
    for md in range(0, 3001, 30):
        if 800 < md and inc < 45:
            inc = min(45.0, inc + 3.0)
        stations.append(Station(md, inc + rng.uniform(-0.2, 0.2) * (inc > 0), 72.0))
    return stations


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=42)
    rng = random.Random(parser.parse_args().seed)
    OUT.mkdir(parents=True, exist_ok=True)

    stations = survey(rng)
    with (OUT / "well_001_survey.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["md_m", "inc_deg", "azi_deg", "tvd_m", "tvdss_m", "north_m", "east_m"])
        for s, p in zip(stations, minimum_curvature(stations), strict=True):
            w.writerow(
                [
                    s.md,
                    round(s.inc, 2),
                    s.azi,
                    round(p.tvd, 2),
                    round(tvdss(p.tvd, RT_ELEVATION_M), 2),
                    round(p.north, 2),
                    round(p.east, 2),
                ]
            )

    t0, depth = datetime(2026, 1, 1, tzinfo=UTC), 2000.0
    with (OUT / "well_001_realtime.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "bit_depth_m", "rop_m_hr", "wob_kn", "spp_bar", "ecd_sg"])
        for i in range(7200):  # 2 h at 1 s
            rop = max(0.0, rng.gauss(18, 3))
            depth += rop / 3600
            w.writerow(
                [
                    (t0 + timedelta(seconds=i)).isoformat(),
                    round(depth, 3),
                    round(rop, 2),
                    round(rng.gauss(120, 8), 1),
                    round(rng.gauss(210, 4), 1),
                    round(rng.gauss(1.32, 0.005), 3),
                ]
            )
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
