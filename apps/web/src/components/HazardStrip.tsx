import { earth } from "@repo/ui";
import { useEffect, useMemo, useRef } from "react";
import { pct, words } from "./common";
import { hms, useNow, useWidth } from "./useWidth";

export type StripCell = { formation: string; hazard: string; mean: number; ci90: [number, number]; n_eff: number; evidence_state: string; band_md: [number, number] | null };
export type StripFormation = { formation: string; top_md: number | null; base_md: number | null; top_tvdss?: number | null };
export type StripBand = { formation: string; hazard: string; mean: number; band_md: [number, number] | null; state: string; distance_m?: number | null; eta_hours?: number | null };

const HAZARD_ORDER = ["loss", "kick", "stuck_pipe", "tight_hole", "pack_off", "washout"];
const tierColor = (p: number) => (p >= 0.5 ? "var(--critical-solid)" : p >= 0.2 ? "var(--warning-solid)" : "var(--advisory-solid)");

/** Exponential average of the live ROP, so the countdown does not jump with every sample. */
function useLiveRop(rop: number | undefined, ts: string | undefined): number | null {
  const ema = useRef<number | null>(null);
  const last = useRef<string | undefined>(undefined);
  if (ts && ts !== last.current && rop != null && Number.isFinite(rop)) {
    last.current = ts;
    ema.current = ema.current == null ? rop : 0.9 * ema.current + 0.1 * rop;
  }
  return ema.current;
}

/**
 * The hazard roadmap as a vertical strip along the well, in measured depth: formations in earth
 * tones, one column per hazard with a bar for the probability, a whisker for the 90 % interval
 * and n_eff, drawn where the offsets had it. The bit, and the next band ahead outlined in gold
 * with a countdown at the live rate of penetration (else the rolling rate).
 */
export function HazardStrip({
  formations,
  cells,
  bands,
  bitMd,
  rollingRate,
  rollingBasis,
  live,
  height = 720,
}: {
  formations: StripFormation[];
  cells: StripCell[];
  bands: StripBand[];
  bitMd: number | null;
  rollingRate: number | null;
  rollingBasis: string;
  live: { rop?: number; bit_md?: number; ts?: string; fresh: boolean };
  height?: number;
}) {
  const [box, width] = useWidth<HTMLDivElement>();
  const now = useNow();
  const liveRop = useLiveRop(live.fresh ? live.rop : undefined, live.ts);
  const forms = formations.filter((f) => f.top_md != null && f.base_md != null);
  const md0 = Math.min(0, ...forms.map((f) => f.top_md!));
  const md1 = Math.max(...forms.map((f) => f.base_md!), bitMd ?? 0);
  const hz = HAZARD_ORDER.filter((h) => cells.some((c) => c.hazard === h && c.mean >= 0.05));
  const top = 28;
  const H = height - top - 12;
  const y = (md: number) => top + ((md - md0) / (md1 - md0 || 1)) * H;
  const axisW = 64;
  const formW = Math.min(170, width * 0.22);
  const colW = Math.max(70, (width - axisW - formW - 12) / Math.max(hz.length, 1));
  const x0 = axisW + formW + 8;

  const bit = live.fresh && live.bit_md != null ? live.bit_md : bitMd;
  const next = useMemo(() => bands.find((b) => b.state === "ahead" || b.state === "in band"), [bands]);
  const rate = liveRop && liveRop > 0.5 ? liveRop : rollingRate;
  const basis = liveRop && liveRop > 0.5 ? `live ROP ${liveRop.toFixed(1)} m/h` : rollingRate ? `${rollingBasis}, ${rollingRate.toFixed(1)} m/h` : null;
  const startedAt = useRef(now);
  useEffect(() => {
    startedAt.current = Date.now();
  }, [bit, rate]);
  const distance = next?.band_md && bit != null ? Math.max(0, next.band_md[0] - bit) : null;
  const etaS = distance != null && rate ? (distance / rate) * 3600 - (now - startedAt.current) / 1000 : null;

  const ticks = useMemo(() => {
    const step = [50, 100, 200, 250, 500, 1000].find((s) => ((md1 - md0) / s) * 28 < H) ?? 1000;
    const out: number[] = [];
    for (let v = Math.ceil(md0 / step) * step; v <= md1; v += step) out.push(v);
    return out;
  }, [md0, md1, H]);

  return (
    <div ref={box} className="w-full">
      {next && (
        <div role="status" aria-live="polite" data-testid="next-hazard" className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-1 rounded-md border-2 border-gold-solid bg-gold-subtle px-4 py-2">
          <span className="text-sm font-semibold uppercase tracking-wide text-gold">Next hazard</span>
          <strong className="text-lg">
            {next.formation} · {words(next.hazard)} <span className="num">{pct(next.mean)}</span>
          </strong>
          {next.state === "in band" ? (
            <span className="font-semibold text-warning">Bit is in the band now</span>
          ) : (
            <>
              <span className="num">{distance == null ? "–" : `${Math.round(distance).toLocaleString()} m ahead`}</span>
              <span className="num text-lg font-semibold" data-testid="eta-countdown">
                ETA {rate ? hms(etaS) : "no rate"}
              </span>
              {basis && <span className="text-sm text-muted">at {basis}</span>}
            </>
          )}
        </div>
      )}
      <svg width={width} height={height} role="img" aria-label="Hazard roadmap along the well: probability, 90 % interval and effective number of offsets per formation and hazard; the bit and the next hazard band." className="block">
        <defs>
          <pattern id="prior-hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <line x1="0" y1="0" x2="0" y2="6" stroke="var(--muted)" strokeWidth="1" opacity="0.5" />
          </pattern>
        </defs>
        {/* depth axis */}
        <text x={4} y={14} className="fill-[var(--muted)] text-[11px]">m MD</text>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={axisW - 6} x2={width} y1={y(t)} y2={y(t)} stroke="var(--border)" strokeWidth={1} />
            <text x={axisW - 10} y={y(t) + 4} textAnchor="end" className="num fill-[var(--muted)] text-[11px]">
              {t.toLocaleString()}
            </text>
          </g>
        ))}
        {/* formations */}
        {forms.map((f, i) => {
          const y0 = y(f.top_md!);
          const h = Math.max(2, y(f.base_md!) - y0);
          return (
            <g key={f.formation}>
              <rect x={axisW} y={y0} width={formW} height={h} fill={earth[i % earth.length]} stroke="var(--border-strong)" strokeWidth={1} />
              {h > 14 && (
                <text x={axisW + 8} y={y0 + Math.min(18, h / 2 + 5)} className="fill-[#3b2f1e] text-[13px] font-semibold">
                  {f.formation}
                </text>
              )}
              {h > 32 && (
                <text x={axisW + 8} y={y0 + 33} className="num fill-[#5a4a33] text-[11px]">
                  top {Math.round(f.top_md!).toLocaleString()} m MD
                </text>
              )}
              {h > 48 && f.top_tvdss != null && (
                <text x={axisW + 8} y={y0 + 47} className="num fill-[#5a4a33] text-[11px]">
                  {Math.round(f.top_tvdss).toLocaleString()} m TVDSS
                </text>
              )}
            </g>
          );
        })}
        {/* hazard columns */}
        {hz.map((h, j) => {
          const cx = x0 + j * colW;
          return (
            <g key={h}>
              <text x={cx + 4} y={18} className="fill-[var(--text)] text-[12px] font-semibold">
                {words(h)}
              </text>
              <line x1={cx} x2={cx} y1={top} y2={top + H} stroke="var(--border)" />
              {cells
                .filter((c) => c.hazard === h && c.mean >= 0.05)
                .map((c) => {
                  const f = forms.find((x) => x.formation === c.formation);
                  if (!f) return null;
                  const [a, b] = c.band_md ?? [f.top_md!, f.base_md!];
                  const y0 = y(a);
                  const hh = Math.max(6, y(b) - y0);
                  const bw = colW - 12;
                  const isNext = next && next.formation === c.formation && next.hazard === c.hazard;
                  const prior = c.evidence_state === "no_evidence";
                  const label = `${pct(c.mean)} (${pct(c.ci90[0])}–${pct(c.ci90[1])}) n ${c.n_eff.toFixed(1)}`;
                  return (
                    <g key={c.formation} data-testid={`strip-${c.formation}-${h}`}>
                      <title>{`${c.formation} ${words(h)}: ${label}${prior ? ", prior only" : ""}`}</title>
                      <rect x={cx + 6} y={y0} width={bw} height={hh} fill={prior ? "url(#prior-hatch)" : "var(--surface-2)"} stroke={isNext ? "var(--gold-solid)" : "var(--border-strong)"} strokeWidth={isNext ? 3 : 1} rx={2} />
                      <rect x={cx + 6} y={y0} width={Math.max(2, bw * c.mean)} height={hh} fill={tierColor(c.mean)} opacity={prior ? 0.45 : 0.85} rx={2} />
                      <line x1={cx + 6 + bw * c.ci90[0]} x2={cx + 6 + bw * c.ci90[1]} y1={y0 + hh / 2} y2={y0 + hh / 2} stroke="var(--text)" strokeWidth={1.5} />
                      {[c.ci90[0], c.ci90[1]].map((v, k) => (
                        <line key={k} x1={cx + 6 + bw * v} x2={cx + 6 + bw * v} y1={y0 + hh / 2 - 4} y2={y0 + hh / 2 + 4} stroke="var(--text)" strokeWidth={1.5} />
                      ))}
                      {hh >= 16 && (
                        <text x={cx + 10} y={y0 + Math.min(hh - 4, 14)} className="num fill-[var(--text)] text-[11px] font-semibold" paintOrder="stroke" stroke="var(--surface)" strokeWidth={3}>
                          {pct(c.mean)} n {c.n_eff.toFixed(1)}
                        </text>
                      )}
                    </g>
                  );
                })}
            </g>
          );
        })}
        {/* bit */}
        {bit != null && (
          <g data-testid="strip-bit">
            <line x1={axisW} x2={width} y1={y(bit)} y2={y(bit)} stroke="var(--cyan-solid)" strokeWidth={2.5} />
            <rect x={axisW + formW - 112} y={y(bit) - 11} width={106} height={20} rx={4} fill="var(--cyan-solid)" />
            <text x={axisW + formW - 59} y={y(bit) + 4} textAnchor="middle" className="num fill-white text-[11px] font-semibold">
              bit {Math.round(bit).toLocaleString()} m
            </text>
          </g>
        )}
      </svg>
    </div>
  );
}
