import { useMemo } from "react";
import { href } from "../router";
import type { Wellbore } from "../state";
import { useWidth } from "./useWidth";

/** A small plan map (equirectangular, north up) of all wells, with some highlighted and
 *  labelled. SVG at its real pixel size, so it stays sharp; each highlighted well is a link. */
export function MiniMap({ wells, highlight, height = 300, label }: { wells: Wellbore[]; highlight: Set<string>; height?: number; label: string }) {
  const [box, width] = useWidth<HTMLDivElement>(600);
  const geo = useMemo(() => {
    const ok = wells.filter((w) => w.lat != null && w.lon != null);
    if (!ok.length) return null;
    const lat0 = ok.reduce((s, w) => s + w.lat!, 0) / ok.length;
    const kx = Math.cos((lat0 * Math.PI) / 180);
    const xs = ok.map((w) => w.lon! * kx);
    const ys = ok.map((w) => w.lat!);
    const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
    const pad = 28;
    const s = Math.min((width - 2 * pad) / Math.max(x1 - x0, 1e-4), (height - 2 * pad) / Math.max(y1 - y0, 1e-4));
    const ox = (width - (x1 - x0) * s) / 2;
    return {
      pts: ok.map((w, i) => ({ w, x: ox + (xs[i] - x0) * s, y: height - pad - (ys[i] - y0) * s })),
      kmPerPx: 111.32 / s,
    };
  }, [wells, width, height]);
  if (!geo) return null;
  const bar = [1, 2, 5, 10, 20].find((km) => km / geo.kmPerPx > 60) ?? 20;
  return (
    <div ref={box} className="w-full">
      <svg width={width} height={height} role="group" aria-label={label} className="block rounded-md border border-line bg-surface-2">
        {geo.pts
          .filter((p) => !highlight.has(p.w.well))
          .map((p) => (
            <circle key={p.w.wellbore_id} cx={p.x} cy={p.y} r={3} fill="var(--muted)" opacity={0.5} />
          ))}
        {geo.pts
          .filter((p) => highlight.has(p.w.well))
          .map((p) => (
            <a key={p.w.wellbore_id} href={href("well", p.w.wellbore_id)} aria-label={`${p.w.well}, block ${p.w.block}`}>
              <circle cx={p.x} cy={p.y} r={6} fill="var(--petrol)" stroke="var(--surface)" strokeWidth={2} />
              <text x={p.x + 8} y={p.y + 4} className="num fill-[var(--text)] text-[12px] font-semibold" paintOrder="stroke" stroke="var(--surface-2)" strokeWidth={3}>
                {p.w.well}
              </text>
            </a>
          ))}
        <g transform={`translate(${12}, ${height - 14})`}>
          <rect width={bar / geo.kmPerPx} height={4} fill="var(--text)" />
          <text y={-5} className="num fill-[var(--muted)] text-[11px]">
            {bar} km
          </text>
        </g>
        <g transform={`translate(${width - 18}, 22)`} aria-label="North up">
          <path d="M0 -12 L5 4 L0 0 L-5 4 Z" fill="var(--text)" />
          <text y={16} textAnchor="middle" className="fill-[var(--text)] text-[11px] font-semibold">
            N
          </text>
        </g>
      </svg>
    </div>
  );
}
