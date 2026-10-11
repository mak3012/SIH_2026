import measuredFile from "../../../docs/eval-report/metrics.json";
import wellsFile from "../../../docs/eval-report/wells.json";

// Every measured number in the UI comes from these files (written by `make evaluation`),
// built in at compile time. No result is typed into the UI by hand.
export const measured = measuredFile as { generated: string; source: string; synthetic: boolean; metrics: Record<string, number | null> };
export type ReplayWell = {
  well: string;
  wellbore_id: number;
  hours: number;
  operating_hours: number;
  offsets: string[];
  false_alerts: number;
  events: { hazard: string; md: number; detected: boolean; detected_warning: boolean; lead_min: number | null; window_min?: number | null }[];
};
export const replayWells = wellsFile as ReplayWell[];

/** A measured value by key, formatted with its unit; "not measured" when absent. */
export function m(key: string, kind: "pct" | "min" | "dur" | "per24h" | "num" = "pct"): string {
  const v = measured.metrics[key];
  if (v == null) return "not measured";
  if (kind === "pct") return `${Math.round(v * 100)} %`;
  if (kind === "min") return `${v > 0 ? "+" : ""}${v.toFixed(1)} min`;
  if (kind === "dur") return `${v.toFixed(1)} min`;
  if (kind === "per24h") return `${v.toFixed(2)} / 24 h`;
  return v.toFixed(2);
}
