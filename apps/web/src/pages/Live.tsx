import {
  Button,
  Card,
  CardBody,
  CardHeader,
  cn,
  EmptyState,
  Field,
  FreshnessIndicator,
  Input,
  Notice,
  Quantity,
  RigStatePill,
  RiskTierBadge,
  RulesAIChip,
  Sheet,
  Skeleton,
  tierOf,
} from "@repo/ui";
import { BellOff, Check, Radio } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { api } from "../api";
import { can } from "../auth";
import { Chart, token, type EOption } from "../components/Chart";
import { ErrorNote, NeedWell, PageHeader, pct, prob, SignInGate, useLoad, words } from "../components/common";
import { Evidence } from "../components/EvidenceViewer";
import { hms, useNow } from "../components/useWidth";
import { useApp, useSelectedWell, type LiveAlert, type Sample } from "../state";

type Envelope = {
  torque_drag: { mu: number; mu_source: string; mw_sg: number; depth_md: number[]; pickup: number[]; slackoff: number[]; rotating: number[]; pickup_hi: number[]; slackoff_hi: number[] };
  pressure_window: { formation: string; top_md: number | null; base_md: number | null; pore_min_sg: number | null; frac_max_sg: number | null; pore_evidence: unknown[]; frac_evidence: unknown[] }[];
};
type Look = { current_md: number | null; rate_m_per_hr: number | null; bands: { formation: string; hazard: string; mean: number; band_md: [number, number] | null; state: string; distance_m?: number | null }[] };

const WINDOW_MIN = 30;
const TH = [0.2, 0.5, 0.8];
// Rig states are categories, not meanings: petrol (data) at falling strength for the drilling
// family, neutrals for the rest. Every segment is also labelled.
const STATES: Record<string, [string, string, number]> = {
  drilling_rotary: ["drilling rotary", "petrol", 1],
  drilling_slide: ["drilling slide", "petrol", 0.7],
  reaming: ["reaming", "petrol", 0.45],
  circulating: ["circulating", "muted", 0.8],
  tripping_in: ["tripping in", "border-strong", 1],
  tripping_out: ["tripping out", "border-strong", 1],
  connection: ["connection", "border", 1],
  static: ["static", "surface-2", 1],
  unknown: ["unknown", "surface-2", 1],
};

/** Ring buffer of samples: the last hour from /history, then the stream. */
function useBuffer(id: number, live: Sample | null): Sample[] {
  const hist = useLoad<{ samples: Sample[] }>(`/active/${id}/history?minutes=60&max_points=1200`);
  const [buf, setBuf] = useState<Sample[]>([]);
  useEffect(() => {
    if (hist.data) setBuf(hist.data.samples);
  }, [hist.data]);
  const last = useRef<string | null>(null);
  useEffect(() => {
    if (!live || live.ts === last.current) return;
    last.current = live.ts;
    setBuf((b) => {
      const cut = Date.parse(live.ts) - 60 * 60_000;
      const out = b.length > 3600 ? b.slice(-3000) : b;
      return [...out.filter((s) => Date.parse(s.ts) >= cut), live];
    });
  }, [live]);
  return buf;
}

function timeSeries(b: EOption, buf: Sample[], lines: { key: string; name: string; color: string; dashed?: boolean }[], unit: string, min?: number): EOption {
  const t1 = buf.length ? Date.parse(buf[buf.length - 1].ts) : Date.now();
  return {
    ...b,
    grid: { left: 52, right: 12, top: 28, bottom: 26 },
    tooltip: { ...(b.tooltip as object), trigger: "axis", valueFormatter: (v: number) => (v == null ? "–" : `${v.toFixed(1)} ${unit}`) },
    legend: { ...(b.legend as object), top: 0, right: 0 },
    xAxis: { ...(b.xAxis as object), type: "time", min: t1 - WINDOW_MIN * 60_000, max: t1, splitNumber: 3, axisLabel: { ...((b.xAxis as { axisLabel: object }).axisLabel), formatter: "{HH}:{mm}", hideOverlap: true } },
    yAxis: { ...(b.yAxis as object), type: "value", name: unit, scale: min == null, min, nameTextStyle: { align: "right" } },
    series: lines.map((l) => ({
      name: l.name,
      type: "line",
      showSymbol: false,
      sampling: "lttb",
      lineStyle: { color: l.color, width: 2, type: l.dashed ? "dashed" : "solid" },
      itemStyle: { color: l.color },
      data: buf.filter((s) => s.values[l.key] != null).map((s) => [Date.parse(s.ts), s.values[l.key]]),
    })),
  };
}

function LiveChart({ title, buf, lines, unit, min, testId }: { title: string; buf: Sample[]; lines: { key: string; name: string; color: string; dashed?: boolean }[]; unit: string; min?: number; testId?: string }) {
  const option = useCallback((b: EOption) => timeSeries(b, buf, lines.map((l) => ({ ...l, color: token(l.color) })), unit, min), [buf, lines, unit, min]);
  const last = buf.at(-1)?.values;
  return (
    <Card>
      <CardHeader
        title={title}
        description={
          <span className="flex flex-wrap gap-x-4 text-fg">
            {lines.map((l) => (
              <span key={l.key}>
                <span className="text-muted">{l.name} </span>
                <Quantity value={last?.[l.key]} unit={unit} digits={unit === "L/min" || unit === "bar" ? 0 : 1} className="text-base font-semibold" />
              </span>
            ))}
          </span>
        }
      />
      <CardBody className="pt-0">
        <Chart option={option} height={180} label={`${title}, last ${WINDOW_MIN} minutes`} testId={testId} />
      </CardBody>
    </Card>
  );
}

function HookloadEnvelope({ env, buf }: { env: Envelope["torque_drag"]; buf: Sample[] }) {
  const option = useCallback(
    (b: EOption): EOption => {
      // Only states the envelope describes: in slips or static the hookload is the block alone.
      const moving = new Set(["drilling_rotary", "drilling_slide", "reaming", "tripping_in", "tripping_out", "circulating"]);
      const pts = buf.filter((s) => moving.has(s.rig_state) && s.values.bit_md != null && s.values.hookload != null);
      const recent = pts.slice(-600);
      const last = pts.at(-1);
      const line = (key: keyof Envelope["torque_drag"], name: string, color: string, dashed = false) => ({
        name,
        type: "line",
        showSymbol: false,
        data: (env[key] as number[]).map((h, i) => [h, env.depth_md[i]]),
        lineStyle: { color, width: dashed ? 1.5 : 2, type: dashed ? "dashed" : "solid" },
        itemStyle: { color },
      });
      const md = recent.map((s) => s.values.bit_md);
      const lo = Math.max(0, Math.min(...md) - 300);
      const hi = Math.max(...md) + 300;
      // x range: the envelope and the measurements inside the depth window shown
      const inWin = env.depth_md.flatMap((d, i) => (d >= lo && d <= hi ? [env.pickup_hi[i], env.slackoff_hi[i]] : []));
      const hx = [...inWin, ...recent.map((s) => s.values.hookload)];
      const x0 = Math.floor((Math.min(...hx) - 30) / 50) * 50;
      const x1 = Math.ceil((Math.max(...hx) + 30) / 50) * 50;
      return {
        ...b,
        // Seven legend entries wrap to two rows: leave them room above the plot.
        grid: { left: 60, right: 16, top: 58, bottom: 36 },
        legend: { ...(b.legend as object), top: 0 },
        tooltip: { ...(b.tooltip as object), trigger: "item" },
        xAxis: { ...(b.xAxis as object), type: "value", name: "hookload kN", nameLocation: "middle", nameGap: 24, min: x0, max: x1 },
        yAxis: { ...(b.yAxis as object), type: "value", inverse: true, name: "bit m MD", nameLocation: "start", min: Math.floor(lo), max: Math.ceil(hi) },
        series: [
          line("pickup", `Pick-up μ ${env.mu.toFixed(2)}`, token("petrol")),
          line("slackoff", "Slack-off", token("petrol")),
          line("rotating", "Rotating", token("muted")),
          line("pickup_hi", "Pick-up μ + 0.1", token("warning"), true),
          line("slackoff_hi", "Slack-off μ + 0.1", token("warning"), true),
          {
            name: "Measured",
            type: "scatter",
            symbolSize: 4,
            itemStyle: { color: token("cyan"), opacity: 0.45 },
            data: recent.map((s) => [s.values.hookload, s.values.bit_md]),
          },
          ...(last
            ? [
                {
                  name: "Now",
                  type: "scatter",
                  symbolSize: 14,
                  itemStyle: { color: token("cyan"), borderColor: token("text"), borderWidth: 2 },
                  data: [[last.values.hookload, last.values.bit_md]],
                  label: { show: true, position: "right", formatter: `${Math.round(last.values.hookload)} kN`, color: token("text"), fontFamily: "IBM Plex Mono", fontWeight: 600 },
                },
              ]
            : []),
        ],
      };
    },
    [env, buf],
  );
  return (
    <Card>
      <CardHeader title="Hookload vs torque-and-drag envelope" description={`Expected pick-up, slack-off and rotating weight against bit depth; friction ${env.mu.toFixed(2)} (${env.mu_source}), mud ${env.mw_sg.toFixed(2)} sg. Outside the dashed lines = more drag than μ + 0.1.`} />
      <CardBody className="pt-0">
        <Chart option={option} height={300} label="Measured hookload against the expected envelope by bit depth" testId="td-envelope" />
      </CardBody>
    </Card>
  );
}

function PressureWindow({ win, buf, bitMd }: { win: Envelope["pressure_window"]; buf: Sample[]; bitMd: number | null }) {
  const option = useCallback(
    (b: EOption): EOption => {
      const rows = win.filter((w) => w.top_md != null && w.base_md != null);
      const step = (k: "pore_min_sg" | "frac_max_sg") => rows.flatMap((w) => (w[k] == null ? [[null, w.top_md], [null, w.base_md]] : [[w[k], w.top_md], [w[k], w.base_md]]));
      const last = buf.at(-1)?.values;
      const ecd = last?.ecd;
      const mw = last?.mw_in;
      const md1 = Math.max(...rows.map((w) => w.base_md!));
      return {
        ...b,
        grid: { left: 64, right: 18, top: 24, bottom: 92 },
        legend: { ...(b.legend as object), bottom: 0 },
        tooltip: { ...(b.tooltip as object), trigger: "item" },
        xAxis: { ...(b.xAxis as object), type: "value", name: "sg", nameLocation: "middle", nameGap: 26, min: 0.95, max: 1.75 },
        yAxis: { ...(b.yAxis as object), type: "value", inverse: true, name: "m MD", nameLocation: "start", min: 0, max: Math.ceil(md1 / 100) * 100 },
        series: [
          { name: "Pore pressure (≥ kick MW)", type: "line", step: false, connectNulls: false, showSymbol: false, data: step("pore_min_sg"), lineStyle: { color: token("advisory"), width: 2.5 }, itemStyle: { color: token("advisory") } },
          { name: "Fracture (≤ loss ECD, LOT)", type: "line", connectNulls: false, showSymbol: false, data: step("frac_max_sg"), lineStyle: { color: token("critical"), width: 2.5 }, itemStyle: { color: token("critical") } },
          {
            name: "Formations",
            type: "line",
            data: [],
            markLine: { silent: true, symbol: "none", lineStyle: { color: token("border-strong"), type: "dotted" }, label: { position: "insideStartTop", formatter: "{b}", color: token("muted"), fontSize: 11 }, data: rows.map((w) => ({ name: w.formation, yAxis: w.top_md })) },
          },
          ...(mw != null ? [{ name: "Mud weight", type: "line", data: [[mw, 0], [mw, bitMd ?? md1]], showSymbol: false, lineStyle: { color: token("petrol"), width: 2.5 }, itemStyle: { color: token("petrol") } }] : []),
          ...(ecd != null && bitMd != null
            ? [{ name: "ECD now", type: "scatter", data: [[ecd, bitMd]], symbolSize: 16, itemStyle: { color: token("cyan"), borderColor: token("text"), borderWidth: 2 }, label: { show: true, position: "right", formatter: `ECD ${ecd.toFixed(2)} sg`, color: token("text"), fontWeight: 600, fontFamily: "IBM Plex Mono" } }]
            : []),
        ],
      };
    },
    [win, buf, bitMd],
  );
  return (
    <Card>
      <CardHeader title="MW / ECD window" description="Per formation from offset evidence: kicks bound pore pressure from below, losses and LOT/FIT bound fracture gradient from above. Live ECD at the bit." />
      <CardBody className="pt-0">
        <Chart option={option} height={340} label="Mud-weight window by depth with live ECD" testId="pressure-window" />
      </CardBody>
    </Card>
  );
}

function RigStateTimeline({ buf }: { buf: Sample[] }) {
  const option = useCallback(
    (b: EOption): EOption => {
      const segs: [number, number, string][] = [];
      for (const s of buf) {
        const t = Date.parse(s.ts);
        const last = segs.at(-1);
        if (last && last[2] === s.rig_state) last[1] = t;
        else segs.push([t, t, s.rig_state]);
      }
      const t1 = segs.at(-1)?.[1] ?? Date.now();
      return {
        ...b,
        grid: { left: 12, right: 12, top: 4, bottom: 24 },
        tooltip: { ...(b.tooltip as object), trigger: "item" },
        xAxis: { ...(b.xAxis as object), type: "time", min: t1 - 60 * 60_000, max: t1, axisLabel: { ...((b.xAxis as { axisLabel: object }).axisLabel), formatter: "{HH}:{mm}" } },
        yAxis: { show: false, min: 0, max: 1 },
        series: [
          {
            type: "custom",
            data: segs.map(([a, z, st]) => [a, z, st]),
            renderItem: (_p: unknown, api: { value: (i: number) => number | string; coord: (v: unknown[]) => number[] }) => {
              const [x0, y0] = api.coord([api.value(0), 1]);
              const [x1, y1] = api.coord([api.value(1), 0]);
              const st = String(api.value(2));
              const [label, color, opacity] = STATES[st] ?? [st, "surface-2", 1];
              const w = Math.max(1, x1 - x0);
              return {
                type: "group",
                children: [
                  { type: "rect", shape: { x: x0, y: y0, width: w, height: y1 - y0 }, style: { fill: token(color), opacity, stroke: token("surface"), lineWidth: 1 } },
                  ...(w > 70 ? [{ type: "text", x: x0 + 4, y: y0 + (y1 - y0) / 2 - 7, style: { text: label, fill: ["petrol", "muted"].includes(color) && opacity > 0.6 ? token("surface") : token("text"), font: "600 12px IBM Plex Sans" } }] : []),
                ],
              };
            },
            tooltip: { formatter: (p: { data: (number | string)[] }) => `${words(String(p.data[2]))}: ${new Date(p.data[0] as number).toLocaleTimeString()}–${new Date(p.data[1] as number).toLocaleTimeString()}` },
          },
        ],
      };
    },
    [buf],
  );
  return (
    <Card>
      <CardHeader title="Rig state, last hour" description="From the rig-state rules (RS-0 to RS-10)." />
      <CardBody className="pt-0">
        <Chart option={option} height={64} label="Rig state over the last hour" testId="rig-state-timeline" />
      </CardBody>
    </Card>
  );
}

function ShelveSheet({ id, hazard, open, onOpenChange }: { id: number; hazard: string; open: boolean; onOpenChange: (o: boolean) => void }) {
  const [form, setForm] = useState({ minutes: 60, reason: "" });
  const [err, setErr] = useState<unknown>(null);
  const [done, setDone] = useState(false);
  async function submit(e: FormEvent) {
    e.preventDefault();
    setErr(null);
    try {
      await api(`/active/${id}/shelves`, { json: { hazard, minutes: form.minutes, reason: form.reason } });
      setDone(true);
    } catch (x) {
      setErr(x);
    }
  }
  return (
    <Sheet open={open} onOpenChange={onOpenChange} title={`Shelve ${words(hazard)}`} description="Advisory and warning alerts for this hazard are recorded but not delivered. Critical alerts are never shelved; at most one shift (720 min).">
      {done ? (
        <Notice tone="advisory" title="Shelved">
          Recorded with your reason in the audit log.
        </Notice>
      ) : (
        <form onSubmit={submit} className="grid gap-4">
          <Field label="Minutes">
            <Input type="number" min={1} max={720} value={form.minutes} onChange={(e) => setForm({ ...form, minutes: Number(e.target.value) })} />
          </Field>
          <Field label="Reason (required)">
            <Input required minLength={5} value={form.reason} onChange={(e) => setForm({ ...form, reason: e.target.value })} placeholder="Pit transfer in progress" />
          </Field>
          <ErrorNote error={err} />
          <Button type="submit" variant="secondary">
            <BellOff /> Shelve
          </Button>
        </form>
      )}
    </Sheet>
  );
}

function AlertCard({ id, a, next, rate }: { id: number; a: LiveAlert | undefined; next: Look["bands"][number] | undefined; rate: number | null }) {
  const { user } = useApp();
  const [acked, setAcked] = useState<number | null>(null);
  const [shelf, setShelf] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const now = useNow();
  const t0 = useRef(now);
  useEffect(() => {
    t0.current = Date.now();
  }, [next?.distance_m, rate]);
  const eta = next?.distance_m != null && rate ? (next.distance_m / rate) * 3600 - (now - t0.current) / 1000 : null;
  if (!a)
    return (
      <Card data-testid="alert-card" className="border-2">
        <CardBody className="flex flex-wrap items-center gap-4">
          <Check aria-hidden className="size-8 text-teal" />
          <div className="flex-1">
            <div className="text-2xl font-semibold">No open alert</div>
            {next && (
              <div className="text-muted">
                Next band: {next.formation} · {words(next.hazard)} <span className="num">{pct(next.mean)}</span>
                {next.distance_m != null && <> in <Quantity value={next.distance_m} unit="m" digits={0} /></>}
                {eta != null && <span className="num"> · ETA {hms(eta)}</span>}
              </div>
            )}
          </div>
        </CardBody>
      </Card>
    );
  const tier = tierOf(a.tier);
  const cleared = !!a.cleared_at;
  const why = a.explanation ?? (a.features ? Object.entries(a.features).filter(([, v]) => v != null).map(([k, v]) => `${words(k)} ${typeof v === "number" ? v.toFixed(2) : v}`).join(" · ") : null);
  return (
    <Card data-testid="alert-card" className={cn("border-l-[10px]", tier === "critical" ? "border-l-critical-solid bg-critical-subtle" : tier === "warning" ? "border-l-warning-solid bg-warning-subtle" : "border-l-advisory-solid bg-advisory-subtle")}>
      <CardBody className="grid gap-3">
        <div className="flex flex-wrap items-center gap-3">
          <RiskTierBadge tier={tier} className="text-base" />
          <h2 className="m-0 text-3xl font-semibold capitalize">{words(a.hazard)}</h2>
          <span className="num text-3xl font-semibold">P {prob(a.posterior)}</span>
          {cleared && <span className="rounded-full border border-line-strong px-3 py-0.5 text-sm">back to normal {new Date(a.cleared_at!).toLocaleTimeString()}</span>}
          <span className="ml-auto">
            <RulesAIChip ai={!!a.explanation} />
          </span>
        </div>
        <dl className="m-0 grid grid-cols-2 gap-x-6 gap-y-1 sm:grid-cols-[1fr_1fr_1fr_1.6fr]">
          <div>
            <dt className="text-sm text-muted">Formation</dt>
            <dd className="m-0 text-lg font-semibold">{a.formation ?? "–"}</dd>
          </div>
          <div>
            <dt className="text-sm text-muted">Raised</dt>
            <dd className="num m-0 text-lg">{a.raised_at ? new Date(a.raised_at).toLocaleTimeString() : "–"}</dd>
          </div>
          <div>
            <dt className="text-sm text-muted">Prior → posterior</dt>
            <dd className="num m-0 text-lg">
              {prob(a.prior)} → {prob(a.posterior)}
            </dd>
          </div>
          <div>
            <dt className="text-sm text-muted">Next band ETA</dt>
            <dd className="num m-0 text-lg">{next ? `${next.formation} ${words(next.hazard)} · ${eta != null ? hms(eta) : "no rate"}` : "–"}</dd>
          </div>
        </dl>
        <p className="m-0 text-lg" data-testid="alert-why">
          <span className="font-semibold">Why: </span>
          {why ?? (a.explanation_status === "pending" ? "Explanation being written…" : "Raised by rules.")}
        </p>
        <div className="flex flex-wrap items-center gap-2">
          {a.citations?.length
            ? a.citations.map((c) => <Evidence key={c.id} c={{ ...c, type: "document" }} />)
            : (a.sources ?? [])
                .filter((s) => s.source_ref_id)
                .map((s) => <Evidence key={s.id} c={{ id: s.id, type: "document", document: s.document ?? undefined, page: s.page ?? undefined, source_ref_id: s.source_ref_id ?? undefined }} />)}
          {can(user, "operate") && a.status !== "acknowledged" && acked !== a.id && (
            <Button className="ml-auto" onClick={() => api(`/alerts/${a.id}/ack`, { method: "POST" }).then(() => setAcked(a.id), setErr)}>
              <Check /> Acknowledge
            </Button>
          )}
          {acked === a.id && <span className="ml-auto font-semibold text-teal">Acknowledged</span>}
          {can(user, "operate") && (
            <Button variant="secondary" onClick={() => setShelf(true)}>
              <BellOff /> Shelve…
            </Button>
          )}
        </div>
        <ErrorNote error={err} />
      </CardBody>
      <ShelveSheet id={id} hazard={a.hazard} open={shelf} onOpenChange={setShelf} />
    </Card>
  );
}

function PosteriorBar({ hazard, p }: { hazard: string; p: number }) {
  const tier = p >= TH[0] ? tierOf(p >= TH[2] ? 3 : p >= TH[1] ? 2 : 1) : null;
  const x = (v: number) => Math.max(0, Math.min(100, ((Math.log10(Math.max(v, 1e-4)) + 4) / 4) * 100));
  return (
    <div className="grid grid-cols-[110px_1fr_70px] items-center gap-3 py-1.5">
      <span className="text-sm">{words(hazard)}</span>
      <div className="relative h-4 rounded-full bg-surface-2" role="img" aria-label={`${words(hazard)} probability ${pct(p)}`}>
        <div className={cn("h-4 rounded-full", tier === "critical" ? "bg-critical-solid" : tier === "warning" ? "bg-warning-solid" : tier === "advisory" ? "bg-advisory-solid" : "bg-cyan-solid")} style={{ width: `${x(p)}%` }} />
        {TH.map((t) => (
          <span key={t} aria-hidden className="absolute top-[-3px] h-[22px] w-0.5 bg-muted" style={{ left: `${x(t)}%` }} />
        ))}
      </div>
      <span className="num text-right text-sm">{prob(p)}</span>
    </div>
  );
}

const CHANNELS: [string, string, string, number][] = [
  ["bit_md", "Bit depth", "m MD", 1],
  ["hole_md", "Hole depth", "m MD", 1],
  ["rop", "ROP", "m/h", 1],
  ["wob", "WOB", "kN", 0],
  ["rpm", "Rotary", "rpm", 0],
  ["mw_in", "Mud weight", "sg", 2],
  ["ecd", "ECD", "sg", 2],
];

function LiveBody({ id }: { id: number }) {
  const { live } = useApp();
  const w = useSelectedWell();
  const buf = useBuffer(id, live.sample);
  const env = useLoad<Envelope>(`/active/${id}/envelope`);
  const look = useLoad<Look>(`/active/${id}/lookahead`);
  const stale = live.feed !== "live";
  const v = { ...(live.sample?.values ?? {}), ...(live.sample?.derived ?? {}) };
  const post = live.sample?.posterior ?? {};
  const alert = useMemo(() => live.alerts.find((a) => !a.cleared_at) ?? live.alerts[0], [live.alerts]);
  const next = look.data?.bands.find((b) => b.state === "ahead" || b.state === "in band");
  const rate = v.rop && v.rop > 0.5 ? v.rop : (look.data?.rate_m_per_hr ?? null);
  const flow = useMemo(() => [{ key: "flow_in", name: "in", color: "petrol" }, { key: "flow_out", name: "out", color: "cyan" }], []);
  const pit = useMemo(() => [{ key: "pit_vol", name: "active pit", color: "cyan" }], []);
  const torque = useMemo(() => [{ key: "torque", name: "measured", color: "cyan" }, { key: "torque_pred", name: "T&D model", color: "petrol", dashed: true }], []);
  const spp = useMemo(() => [{ key: "spp", name: "SPP", color: "cyan" }], []);
  return (
    <>
      <PageHeader
        title={`Live · ${w?.wellbore ?? ""}`}
        description="One-way, read-only feed from the rig. Alerts are raised by rules and calibrated detectors; AI may add the explanation."
        badge={
          <span data-testid="feed-state">
            <FreshnessIndicator state={live.feed === "connecting" ? "disconnected" : live.feed} />
          </span>
        }
      />
      {live.feed === "none" ? (
        <Card>
          <EmptyState icon={<Radio />} title="No real-time feed for this well">
            Start one with <span className="num">make realtime</span> or replay a well with <span className="num">make replay WELL=… SPEED=300</span>.
          </EmptyState>
        </Card>
      ) : (
        <div data-testid="live-well" data-feed-state={live.feed} className={cn("grid gap-4 transition-[filter,opacity] duration-base", stale && "opacity-60 grayscale")}>
          {stale && (
            <Notice tone="warning" title={`DATA ${live.feed.toUpperCase()}`}>
              No new sample: the numbers below are not current.
            </Notice>
          )}
          <AlertCard id={id} a={alert} next={next} rate={rate} />
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
            <RigStatePill state={live.sample?.rig_state} />
            <span className="text-muted">{live.sample?.formation ?? "formation unknown"}</span>
            {CHANNELS.map(([k, label, unit, d]) => (
              <span key={k} className="flex items-baseline gap-2">
                <span className="text-xs uppercase tracking-wide text-muted">{label}</span>
                <Quantity value={v[k]} unit={unit} digits={d} className="text-xl font-semibold" />
              </span>
            ))}
          </div>
          {buf.length === 0 ? (
            <Skeleton className="h-64 w-full" />
          ) : (
            <>
              <RigStateTimeline buf={buf} />
              <div className="grid gap-4 lg:grid-cols-2 2xl:grid-cols-4">
                <LiveChart title="Flow in / out" buf={buf} lines={flow} unit="L/min" min={0} testId="chart-flow" />
                <LiveChart title="Active pit" buf={buf} lines={pit} unit="m³" testId="chart-pit" />
                <LiveChart title="Torque" buf={buf} lines={torque} unit="kN·m" min={0} testId="chart-torque" />
                <LiveChart title="Standpipe pressure" buf={buf} lines={spp} unit="bar" min={0} testId="chart-spp" />
              </div>
              <div className="grid gap-4 xl:grid-cols-2">
                {env.data ? <HookloadEnvelope env={env.data.torque_drag} buf={buf} /> : <ErrorNote error={env.error} onRetry={env.reload} />}
                {env.data ? <PressureWindow win={env.data.pressure_window} buf={buf} bitMd={v.bit_md ?? null} /> : env.loading ? <Skeleton className="h-80" /> : null}
              </div>
            </>
          )}
          <Card>
            <CardHeader title="Hazard probability now" description="Posterior = roadmap prior × detector likelihood ratio, on a log scale from 0.01 % to 100 %. Marks: advisory 20 %, warning 50 %, critical 80 %." actions={<RulesAIChip ai={false} className="shrink-0" />} />
            <CardBody>
              {Object.keys(post).length === 0 ? <p className="m-0 text-muted">Waiting for two minutes of data.</p> : Object.entries(post).map(([h, p]) => <PosteriorBar key={h} hazard={h} p={p} />)}
            </CardBody>
          </Card>
        </div>
      )}
    </>
  );
}

export function LivePage({ id }: { id: number | null }) {
  const { selected, select } = useApp();
  useEffect(() => {
    if (id != null && id !== selected) select(id);
  }, [id, selected, select]);
  return (
    <SignInGate>
      <NeedWell id={id ?? selected}>{(i) => <LiveBody id={i} />}</NeedWell>
    </SignInGate>
  );
}
