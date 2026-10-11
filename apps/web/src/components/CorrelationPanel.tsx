import { earth, Field, NativeSelect } from "@repo/ui";
import { useCallback, useMemo, useState } from "react";
import type { CustomSeriesRenderItemAPI } from "echarts";
import { Chart, token, type EOption } from "./Chart";
import { words } from "./common";
import { EvidenceSheet, type Citation } from "./EvidenceViewer";

type Pt = { md: number; tvdss: number | null };
export type CorrEvent = Pt & { id: number; type: string; severity: number | null; formation: string | null; narrative: string | null; mw: number | null; ecd: number | null; loss_bbl: number | null; npt_hr: number | null; document: string | null; document_id: number | null; page: number | null; bbox: number[] | null; review_status: string };
export type Track = {
  wellbore_id: number;
  well: string;
  weight?: number;
  td_md: number | null;
  td_tvdss: number | null;
  tops: (Pt & { formation: string })[];
  events: CorrEvent[];
  casing: (Pt & { kind: string; od_in: number })[];
  mud: (Pt & { mw: number })[];
  ecd: (Pt & { ecd: number })[];
  lot: (Pt & { type: string; emw: number })[];
  prognosis?: (Pt & { formation: string })[];
  bands?: { hazard: string; formation: string; mean: number; ci90: number[]; md: number[]; tvdss: (number | null)[] }[];
  bit?: Pt | null;
};
export type Correlation = { wellbore_id: number; formations: string[]; datum: string | null; datum_choices: string[]; tracks: Track[] };

const SG = [1.0, 2.0]; // MW/ECD column range
const LITH = 0.5; // share of a track for the formation column
const CURVE0 = 0.54;
const CURVE1 = 0.9;
const ICON: Record<string, string> = {
  loss: "path://M5,0 C5,0 10,6 10,9 A5,5 0 1 1 0,9 C0,6 5,0 5,0 Z", // droplet: fluid lost
  kick: "triangle", // pointing up: fluid in
  stuck_pipe: "rect",
};
const BAND_ORDER = ["loss", "kick", "stuck_pipe", "tight_hole", "pack_off", "washout"];
const ODS: Record<string, string> = { "20": "20", "13.375": "13⅜", "9.625": "9⅝", "7": "7", "5.5": "5½", "4.5": "4½" };

function frac(od: number): string {
  return `${ODS[String(od)] ?? od} in`;
}

/** Correlation of the active well and its offsets: depth tracks flattened on a formation top. */
export function CorrelationPanel({ data, height = 720 }: { data: Correlation; height?: number }) {
  const [datum, setDatum] = useState(data.datum);
  const [open, setOpen] = useState<Citation | null>(null);

  const tracks = data.tracks;
  const offsetOf = useCallback(
    (t: Track) => {
      const d = [...t.tops, ...(t.prognosis ?? [])].find((x) => x.formation === datum);
      return d?.tvdss ?? null;
    },
    [datum],
  );

  const option = useCallback(
    (b: EOption): EOption => {
      const text = token("text");
      const muted = token("muted");
      const surface = token("surface");
      const gold = token("gold-solid");
      const cyan = token("cyan");
      const petrol = token("petrol");
      const sepia = token("sepia-solid");
      const tierCol = (p: number) => token(p >= 0.5 ? "critical-solid" : p >= 0.2 ? "warning-solid" : "advisory-solid");
      const order = data.formations;
      const rel = (t: Track, v: number | null) => {
        const o = offsetOf(t);
        return v == null || o == null ? null : v - o;
      };
      const forms: number[][] = [];
      const formNames: string[] = [];
      const shoes: number[][] = [];
      const shoeNames: string[] = [];
      const bands: number[][] = [];
      const bandNames: string[] = [];
      const headers: number[][] = [];
      const headerText: [string, string][] = [];
      const lanes = BAND_ORDER.filter((h) => tracks.some((t) => (t.bands ?? []).some((b) => b.hazard === h)));
      const curveX = (i: number, sg: number) => i + CURVE0 + ((Math.min(Math.max(sg, SG[0]), SG[1]) - SG[0]) / (SG[1] - SG[0])) * (CURVE1 - CURVE0);
      let lo = 0;
      let hi = 0;
      const series: unknown[] = [];
      tracks.forEach((t, i) => {
        const tops = (t.tops.length ? t.tops : (t.prognosis ?? [])).filter((x) => x.tvdss != null);
        const bottom = rel(t, t.td_tvdss);
        tops.forEach((top, k) => {
          const a = rel(t, top.tvdss)!;
          const z = k + 1 < tops.length ? rel(t, tops[k + 1].tvdss)! : (bottom ?? a + 100);
          lo = Math.min(lo, a);
          hi = Math.max(hi, z);
          forms.push([i, a, z, order.indexOf(top.formation)]);
          formNames.push(top.formation);
        });
        headers.push([i]);
        headerText.push([t.well, t.weight != null ? `weight ${t.weight.toFixed(2)}` : i === 0 ? "this well" : "nearby"]);
        t.casing.forEach((c) => {
          const v = rel(t, c.tvdss);
          if (v != null) {
            shoes.push([i, v, c.md]);
            shoeNames.push(frac(c.od_in));
          }
        });
        (t.bands ?? []).forEach((bd) => {
          const a = rel(t, bd.tvdss[0]);
          const z = rel(t, bd.tvdss[1]);
          if (a != null && z != null) {
            bands.push([i, a, z, bd.mean, lanes.indexOf(bd.hazard)]);
            bandNames.push(`${words(bd.hazard)} ${Math.round(bd.mean * 100)} %`);
          }
        });
        const mud = t.mud.filter((m) => m.tvdss != null).map((m) => [curveX(i, m.mw), rel(t, m.tvdss), m.mw, m.md]);
        if (mud.length)
          series.push({
            name: "Mud weight",
            type: "line",
            step: "end",
            data: mud,
            symbol: "none",
            lineStyle: { color: petrol, width: 2 },
            tooltip: { formatter: (p: { data: number[] }) => `${t.well} mud weight <b>${p.data[2].toFixed(2)} sg</b> at ${Math.round(p.data[3])} m MD` },
            z: 4,
          });
        const ecd = t.ecd.filter((m) => m.tvdss != null).map((m) => [curveX(i, m.ecd), rel(t, m.tvdss), m.ecd, m.md]);
        if (ecd.length)
          series.push({
            name: "ECD",
            type: "scatter",
            data: ecd,
            symbol: "diamond",
            symbolSize: 9,
            itemStyle: { color: surface, borderColor: petrol, borderWidth: 2 },
            tooltip: { formatter: (p: { data: number[] }) => `${t.well} ECD <b>${p.data[2].toFixed(2)} sg</b> at ${Math.round(p.data[3])} m MD` },
            z: 5,
          });
        const lot = t.lot.filter((m) => m.tvdss != null).map((m) => [curveX(i, m.emw), rel(t, m.tvdss), m.emw, m.md, m.type.toUpperCase()]);
        if (lot.length)
          series.push({
            name: "LOT / FIT",
            type: "scatter",
            data: lot,
            symbol: "pin",
            symbolSize: 14,
            itemStyle: { color: sepia },
            tooltip: { formatter: (p: { data: (number | string)[] }) => `${t.well} ${p.data[4]} <b>${(p.data[2] as number).toFixed(2)} sg</b> at ${Math.round(p.data[3] as number)} m MD` },
            z: 5,
          });
        const evs = t.events
          .map((e) => ({ e, v: rel(t, e.tvdss) }))
          .filter((x) => x.v != null)
          .map(({ e, v }) => ({
            value: [i + LITH * 0.72, v],
            symbol: ICON[e.type] ?? "circle",
            symbolSize: 10 + 2 * (e.severity ?? 1),
            symbolRotate: e.type === "kick" ? 0 : 0,
            itemStyle: { color: gold, borderColor: text, borderWidth: 1.2 },
            e,
            well: t.well,
          }));
        if (evs.length)
          series.push({
            name: "Events",
            type: "scatter",
            data: evs,
            z: 8,
            cursor: "pointer",
            tooltip: {
              formatter: (p: { data: { e: CorrEvent; well: string } }) => {
                const e = p.data.e;
                const bits = [
                  `<b>${p.data.well} · ${words(e.type)}</b>${e.severity ? ` · severity ${e.severity}` : ""}`,
                  `${Math.round(e.md).toLocaleString()} m MD${e.formation ? ` · ${e.formation}` : ""}`,
                  e.mw ? `MW ${e.mw.toFixed(2)} sg${e.ecd ? `, ECD ${e.ecd.toFixed(2)} sg` : ""}` : "",
                  e.loss_bbl ? `${e.loss_bbl} bbl lost` : "",
                  e.npt_hr ? `${e.npt_hr} h NPT` : "",
                  e.narrative ? `<i>${e.narrative.slice(0, 160)}</i>` : "",
                  e.document ? `${e.document} p. ${e.page} · click to open the page` : "",
                ];
                return bits.filter(Boolean).join("<br/>");
              },
            },
          });
        if (t.bit?.tvdss != null) {
          const v = rel(t, t.bit.tvdss)!;
          series.push({
            name: "Bit",
            type: "line",
            data: [
              [i, v],
              [i + CURVE1, v],
            ],
            symbol: "none",
            lineStyle: { color: cyan, width: 3 },
            itemStyle: { color: cyan },
            endLabel: { show: true, formatter: `bit ${Math.round(t.bit.md)} m`, color: cyan, fontWeight: 600, fontFamily: "IBM Plex Mono", backgroundColor: surface, padding: [1, 3] },
            tooltip: { formatter: `Bit at ${Math.round(t.bit.md)} m MD` },
            z: 9,
          });
        }
      });
      const pad = (hi - lo) * 0.04;
      series.unshift(
        {
          name: "Formations",
          type: "custom",
          data: forms,
          z: 1,
          silent: true,
          renderItem: (p: { dataIndex: number }, api: CustomSeriesRenderItemAPI) => {
            const i = api.value(0) as number;
            const [x0, y0] = api.coord([i, api.value(1)]);
            const [x1, y1] = api.coord([i + LITH, api.value(2)]);
            const [cx0] = api.coord([i + CURVE0, 0]);
            const [cx1] = api.coord([i + CURVE1, 0]);
            const idx = api.value(3) as number;
            const name = formNames[p.dataIndex];
            const h = y1 - y0;
            return {
              type: "group",
              children: [
                { type: "rect", shape: { x: x0, y: y0, width: x1 - x0, height: h }, style: { fill: earth[(idx < 0 ? 0 : idx) % earth.length], stroke: "#8a7a5c", lineWidth: 1 } },
                { type: "rect", shape: { x: cx0, y: y0, width: cx1 - cx0, height: h }, style: { fill: "transparent", stroke: token("border"), lineWidth: 1 } },
                ...(h > 16 ? [{ type: "text", x: x0 + 5, y: y0 + 4, style: { text: name, fill: "#3b2f1e", font: "600 12px IBM Plex Sans", width: x1 - x0 - 8, overflow: "truncate" } }] : []),
              ],
            };
          },
        },
        {
          name: "Hazard bands (this well)",
          type: "custom",
          data: bands,
          z: 3,
          renderItem: (p: { dataIndex: number }, api: CustomSeriesRenderItemAPI) => {
            const i = api.value(0) as number;
            const lane = api.value(4) as number;
            const w = (LITH * 0.55) / Math.max(lanes.length, 1);
            const [x0, y0] = api.coord([i + LITH * 0.45 + lane * w, api.value(1)]);
            const [x1, y1] = api.coord([i + LITH * 0.45 + (lane + 1) * w, api.value(2)]);
            const color = tierCol(api.value(3) as number);
            const h = Math.max(4, y1 - y0);
            const [name, pc] = bandNames[p.dataIndex].split(/ (?=\d+ %$)/);
            return {
              type: "group",
              children: [
                { type: "rect", shape: { x: x0 + 1, y: y0, width: x1 - x0 - 2, height: h }, style: { fill: color, opacity: 0.6, stroke: color, lineWidth: 1.5 } },
                ...(h >= 30 && x1 - x0 >= 26
                  ? [{ type: "text", x: x0 + 3, y: y0 + 3, style: { text: `${name}\n${pc}`, fill: text, font: "600 10px IBM Plex Sans", stroke: surface, lineWidth: 3, lineHeight: 12 } }]
                  : []),
              ],
            };
          },
          tooltip: { formatter: (p: { dataIndex: number }) => `Projected hazard band (this well): ${bandNames[p.dataIndex]}` },
        },
        {
          name: "Casing shoes",
          type: "custom",
          data: shoes,
          z: 6,
          renderItem: (p: { dataIndex: number }, api: CustomSeriesRenderItemAPI) => {
            const i = api.value(0) as number;
            const [x, y] = api.coord([i + LITH, api.value(1)]);
            return {
              type: "group",
              children: [
                { type: "polygon", shape: { points: [[x, y], [x + 9, y], [x, y - 10]] }, style: { fill: text } },
                { type: "line", shape: { x1: x, y1: y - 40, x2: x, y2: y }, style: { stroke: text, lineWidth: 2 } },
                { type: "text", x: x + 11, y: y - 12, style: { text: shoeNames[p.dataIndex], fill: text, font: "11px IBM Plex Mono", stroke: surface, lineWidth: 3 } },
              ],
            };
          },
          tooltip: { formatter: (p: { dataIndex: number; data: number[] }) => `Casing shoe ${shoeNames[p.dataIndex]} at ${Math.round(p.data[2])} m MD` },
        },
        {
          name: "Track headers",
          type: "custom",
          data: headers,
          silent: true,
          z: 10,
          renderItem: (p: { dataIndex: number }, api: CustomSeriesRenderItemAPI) => {
            const i = api.value(0) as number;
            const [x] = api.coord([i, 0]);
            const [well, sub] = headerText[p.dataIndex];
            return {
              type: "group",
              children: [
                { type: "text", x, y: 8, style: { text: well, fill: text, font: "600 13px IBM Plex Mono" } },
                { type: "text", x, y: 26, style: { text: sub, fill: muted, font: "11px IBM Plex Sans" } },
              ],
            };
          },
        },
      );
      return {
        ...b,
        grid: { left: 64, right: 24, top: 52, bottom: 40 },
        legend: { ...(b.legend as object), bottom: 0, data: ["Mud weight", "ECD", "LOT / FIT", "Events", "Bit"] },
        tooltip: { ...(b.tooltip as object), trigger: "item" },
        xAxis: { ...(b.xAxis as object), type: "value", min: 0, max: tracks.length, axisLabel: { show: false }, splitLine: { show: false }, axisTick: { show: false } },
        yAxis: {
          ...(b.yAxis as object),
          type: "value",
          inverse: true,
          min: Math.floor((lo - pad) / 100) * 100,
          max: Math.ceil((hi + pad) / 100) * 100,
          name: `m from ${datum ?? "datum"} top (TVDSS)`,
          nameLocation: "middle",
          nameGap: 46,
        },
        series: [
          ...series,
          {
            name: "Datum",
            type: "line",
            data: [],
            markLine: { silent: true, symbol: "none", lineStyle: { color: text, type: "dashed", width: 1.5 }, label: { formatter: datum ?? "", color: text, position: "insideStartTop" }, data: [{ yAxis: 0 }] },
          },
        ],
      };
    },
    [tracks, data.formations, datum, offsetOf],
  );

  const lanes = useMemo(() => BAND_ORDER.filter((h) => tracks.some((t) => (t.bands ?? []).some((b) => b.hazard === h))), [tracks]);
  const missing = useMemo(() => tracks.filter((t) => offsetOf(t) == null).map((t) => t.well), [tracks, offsetOf]);
  return (
    <div className="grid gap-3">
      <div className="flex flex-wrap items-end gap-4">
        <Field label="Flatten on">
          <NativeSelect value={datum ?? ""} onChange={(e) => setDatum(e.target.value)} data-testid="datum-select">
            {data.formations.map((f) => (
              <option key={f} value={f} disabled={!data.datum_choices.includes(f)}>
                {f} top{data.datum_choices.includes(f) ? "" : " (not in every well)"}
              </option>
            ))}
          </NativeSelect>
        </Field>
        <p className="m-0 pb-2 text-sm text-muted">
          Formation column on the left of each track; mud weight (line), ECD (◆) and LOT/FIT (pin) on the right, <span className="num">1.0–2.0 sg</span>. Events in gold: droplet loss, triangle kick, square stuck pipe; click one to open its page.
          {lanes.length > 0 && <> This well&apos;s projected hazard bands, in lanes left to right: {lanes.map(words).join(", ")}.</>}
        </p>
      </div>
      {missing.length > 0 && <p className="m-0 text-sm text-warning">Not shown (no {datum} top): {missing.join(", ")}</p>}
      <div className="overflow-x-auto">
      <div style={{ minWidth: 90 + tracks.length * 150 }}>
      <Chart
        option={option}
        height={height}
        label={`Correlation of ${tracks.map((t) => t.well).join(", ")} flattened on the ${datum} top`}
        testId="correlation-chart"
        onClick={(p) => {
          const d = p.data as { e?: CorrEvent } | undefined;
          const e = d?.e;
          if (e?.document_id && e.page) setOpen({ id: `EV${e.id}`, type: "document", document: e.document ?? "document", document_id: e.document_id, page: e.page, bbox: e.bbox, review_status: e.review_status });
        }}
      />
      </div>
      </div>
      <EvidenceSheet c={open} onOpenChange={(o) => !o && setOpen(null)} />
    </div>
  );
}
