import { Button, Card, CardBody, CardHeader, DataTable, Field, NativeSelect, Notice, RiskTierBadge, Skeleton, Stat } from "@repo/ui";
import { Pause, Play, RotateCcw } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Chart, token, type EOption } from "../components/Chart";
import { ErrorNote, PageHeader, prob, words } from "../components/common";
import { m, measured, replayWells } from "../measured";

type Timeline = {
  well: string;
  wellbore_id: number;
  start: string;
  step_s: number;
  t: number[];
  md: (number | null)[];
  state: (string | null)[];
  formation: (string | null)[];
  post: Record<string, number[]>;
  thresholds: Record<string, number>;
  episodes: { hazard: string; start: number; end: number; md: number }[];
  alerts: { hazard: string; t: number; tier: number; kind: string; posterior: number }[];
  events: { hazard: string; md: number; detected: boolean; detected_warning: boolean; lead_min: number | null; window_min?: number | null }[];
  false_alerts: { hazard: string; t: number }[];
  operating_hours: number;
};

// Built in from docs/eval-report/timelines (written by `make evaluation`); one lazy chunk per well.
const FILES = import.meta.glob<Timeline>("../../../../docs/eval-report/timelines/*.json", { import: "default" });
const load = (well: string) => FILES[`../../../../docs/eval-report/timelines/${well}.json`]?.();

const SPEEDS = [60, 120, 300, 600];
const HAZ = ["loss", "kick", "stuck_pipe", "tight_hole", "pack_off", "washout"];
const hh = (s: number) => `${Math.floor(s / 3600)}:${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}`;

function RiskStrips({ tl, cursor }: { tl: Timeline; cursor: number }) {
  const hazards = HAZ.filter((h) => tl.post[h]);
  const option = useCallback(
    (b: EOption): EOption => {
      const n = hazards.length;
      const top = 16;
      const rowH = 86;
      const cut = tl.t.findIndex((t) => t > cursor);
      const upto = cut < 0 ? tl.t.length : cut;
      const tier = (t: number) => token(t >= 3 ? "critical-solid" : t >= 2 ? "warning-solid" : "advisory-solid");
      const axis = b.xAxis as Record<string, unknown>;
      return {
        ...b,
        tooltip: { ...(b.tooltip as object), trigger: "axis", valueFormatter: (v: number) => prob(v) },
        legend: { show: false }, // each strip is named on its axis
        grid: hazards.map((_, k) => ({ left: 96, right: 20, top: top + k * rowH, height: rowH - 22 })),
        xAxis: hazards.map((_, k) => ({
          ...axis,
          type: "value",
          gridIndex: k,
          min: 0,
          max: tl.t.at(-1),
          axisLabel: { ...(axis.axisLabel as object), show: k === n - 1, formatter: (v: number) => hh(v) },
          splitLine: { show: false },
        })),
        yAxis: hazards.map((h, k) => ({
          ...(b.yAxis as object),
          type: "log",
          gridIndex: k,
          min: 1e-4,
          max: 1,
          name: words(h),
          nameLocation: "middle",
          nameGap: 58,
          nameRotate: 0,
          nameTextStyle: { color: token("text"), fontWeight: 600, fontSize: 12, align: "right" },
          axisLabel: { ...((b.yAxis as Record<string, object>).axisLabel), formatter: (v: number) => (v >= 0.01 ? `${v * 100} %` : ""), fontSize: 10 },
          splitLine: { show: false },
        })),
        series: hazards.map((h, k) => ({
          name: words(h),
          type: "line",
          xAxisIndex: k,
          yAxisIndex: k,
          showSymbol: false,
          data: tl.t.slice(0, upto).map((t, i) => [t, Math.max(tl.post[h][i], 1e-4)]),
          lineStyle: { color: token("cyan"), width: 1.6 },
          itemStyle: { color: token("cyan") },
          areaStyle: { color: token("cyan"), opacity: 0.08 },
          markLine: {
            silent: true,
            symbol: "none",
            data: [
              ...Object.entries(tl.thresholds).map(([name, v]) => ({ yAxis: v, lineStyle: { color: token(`${name === "critical" ? "critical" : name === "warning" ? "warning" : "advisory"}-solid`), type: "dashed", width: 1 }, label: { show: false } })),
              { xAxis: cursor, lineStyle: { color: token("text"), width: 1.5 }, label: { show: false } },
            ],
          },
          markArea: {
            silent: true,
            itemStyle: { color: token("gold-solid"), opacity: 0.22 },
            data: tl.episodes.filter((e) => e.hazard === h).map((e) => [{ xAxis: e.start, name: "event" }, { xAxis: e.end }]),
            label: { show: true, position: "insideTop", color: token("gold"), fontWeight: 600, fontSize: 11 },
          },
          markPoint: {
            symbol: "pin",
            symbolSize: 26,
            data: tl.alerts
              .filter((a) => a.hazard === h && a.t <= cursor)
              .map((a) => ({ coord: [a.t, Math.max(a.posterior, 1e-4)], itemStyle: { color: tier(a.tier) }, value: a.tier === 3 ? "C" : a.tier === 2 ? "W" : "A" })),
            label: { color: "#fff", fontWeight: 700, fontSize: 10 },
          },
        })),
      };
    },
    [tl, cursor, hazards],
  );
  return <Chart option={option} height={16 + hazards.length * 86 + 20} label={`Risk score per hazard over the replay of ${tl.well}, with alert thresholds, events and alerts`} testId="replay-chart" />;
}

function Studio({ well }: { well: string }) {
  const [tl, setTl] = useState<Timeline | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const [cursor, setCursor] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(300);
  const last = useRef<number | null>(null);

  useEffect(() => {
    setTl(null);
    setCursor(0);
    setPlaying(false);
    const p = load(well);
    if (!p) return setErr(new Error(`No replay timeline for ${well}: run make evaluation.`));
    p.then(setTl, setErr);
  }, [well]);

  useEffect(() => {
    if (!playing || !tl) return;
    let raf = 0;
    const tick = (now: number) => {
      const dt = last.current == null ? 0 : (now - last.current) / 1000;
      last.current = now;
      setCursor((c) => {
        const end = tl.t.at(-1) ?? 0;
        const next = Math.min(end, c + dt * speed);
        if (next >= end) setPlaying(false);
        return next;
      });
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(raf);
      last.current = null;
    };
  }, [playing, speed, tl]);

  const i = useMemo(() => (tl ? Math.max(0, tl.t.findIndex((t) => t > cursor) - 1) : 0), [tl, cursor]);
  if (err) return <ErrorNote error={err} />;
  if (!tl) return <Skeleton className="h-[560px] w-full" />;
  const end = tl.t.at(-1) ?? 0;
  const done = cursor >= end;
  // events[k] is the scoring of episodes[k] (api.replay.metrics keeps the order)
  const seenEvents = tl.events.map((e, k) => ({ e, ep: tl.episodes[k] }));
  return (
    <div className="grid gap-4">
      <Card>
        <CardBody className="flex flex-wrap items-end gap-4">
          <Button onClick={() => (done ? (setCursor(0), setPlaying(true)) : setPlaying(!playing))} aria-pressed={playing} data-testid="replay-play">
            {playing ? <Pause /> : <Play />} {playing ? "Pause" : done ? "Play again" : "Play"}
          </Button>
          <Button variant="secondary" onClick={() => (setPlaying(false), setCursor(0))}>
            <RotateCcw /> Reset
          </Button>
          <Field label="Speed">
            <NativeSelect value={speed} onChange={(e) => setSpeed(Number(e.target.value))} data-testid="replay-speed">
              {SPEEDS.map((s) => (
                <option key={s} value={s}>
                  {s}× ({s / 60} min of well time per second)
                </option>
              ))}
            </NativeSelect>
          </Field>
          <label className="grid min-w-[240px] flex-1 gap-1 text-sm">
            <span className="font-medium">
              Well time <span className="num">{hh(cursor)}</span> of <span className="num">{hh(end)}</span>
            </span>
            <input type="range" min={0} max={end} step={tl.step_s} value={cursor} onChange={(e) => (setPlaying(false), setCursor(Number(e.target.value)))} className="min-h-target" aria-label="Well time" />
          </label>
          <div className="text-sm" aria-live="off">
            <div className="num">{tl.md[i] != null ? `${Math.round(tl.md[i]!).toLocaleString()} m MD` : "–"}</div>
            <div className="text-muted">
              {tl.formation[i] ?? "–"} · {words(tl.state[i] ?? "–")}
            </div>
          </div>
        </CardBody>
      </Card>
      <Card>
        <CardHeader title={`${tl.well}, held out`} description="Risk score per hazard (log scale) as the live path computed it, with priors from the other wells only. Dashed lines: the advisory (blue, 20 %), warning (orange, 50 %) and critical (red, 80 %) thresholds. Gold: the recorded event. Pins: alerts as delivered (A/W/C)." />
        <CardBody>
          <RiskStrips tl={tl} cursor={cursor} />
        </CardBody>
      </Card>
      <Card data-testid="replay-results">
        <CardHeader title="Measured on this well" description={done ? "Replay finished." : "Each result appears when the replay reaches the event."} />
        <CardBody className="grid gap-4">
          <div className="flex flex-wrap gap-6">
            <Stat label="Operating hours">{tl.operating_hours.toFixed(1)} h</Stat>
            <Stat label="False alerts">{done ? tl.false_alerts.length : tl.false_alerts.filter((a) => a.t <= cursor).length}</Stat>
            <Stat label="Events detected">
              {tl.events.filter((e, k) => e.detected && (seenEvents[k].ep?.start ?? 0) <= cursor).length} / {tl.events.length}
            </Stat>
          </div>
          {tl.events.length === 0 ? (
            <p className="m-0 text-muted">No recorded hazard event in this well: any alert is a false alert.</p>
          ) : (
            <DataTable
              caption="Events in this well"
              head={["Event", "Depth", "Detected", "Lead time", "Decision window"]}
              rows={seenEvents.map(({ e, ep }) => {
                const shown = done || (ep?.start ?? 0) <= cursor;
                return [
                  words(e.hazard),
                  <span key="d" className="num">{Math.round(e.md).toLocaleString()} m MD</span>,
                  shown ? (e.detected_warning ? <RiskTierBadge key="t" tier="warning">{" or above"}</RiskTierBadge> : e.detected ? <RiskTierBadge key="t" tier="advisory" /> : <span key="t" className="font-semibold text-critical">missed</span>) : <span key="t" className="text-muted">…</span>,
                  shown && e.lead_min != null ? <span key="l" className="num">{e.lead_min > 0 ? "+" : ""}{e.lead_min.toFixed(1)} min{e.lead_min < 0 ? " (after onset)" : ""}</span> : "–",
                  shown && e.window_min != null ? <span key="w" className="num" data-testid="decision-window">{e.window_min.toFixed(1)} min</span> : "–",
                ];
              })}
            />
          )}
        </CardBody>
      </Card>
    </div>
  );
}

export function ReplayPage() {
  const x = measured.metrics;
  const [well, setWell] = useState(() => replayWells.find((w) => w.events.length > 1)?.well ?? replayWells[0]?.well ?? "");
  return (
    <>
      <PageHeader
        title="Replay studio"
        description="Leave-one-well-out: each well streamed through the live path with priors from the other wells only. Pick a held-out well, play it at 60–600×, watch the risk cross the thresholds."
      />
      {measured.synthetic && (
        <Notice tone="gold" title="Measured on synthetic data">
          Generated <span className="num">{new Date(measured.generated).toLocaleString()}</span> from {measured.source}. Rerun on real wells before relying on them.
        </Notice>
      )}
      <div className="mt-4 grid gap-4">
        <Card>
          <CardHeader title="All wells" description={`${x.replay_wells ?? "–"} wells, ${x.replay_operating_hours ?? "–"} operating hours, ${x.alert_events ?? "–"} hazard events.`} />
          <CardBody data-testid="alert-metrics" className="flex flex-wrap gap-6">
            <Stat label="Events alerted (any tier)" accent="cyan">{m("alert_event_recall")}</Stat>
            <Stat label="Alerted at warning or above" accent="cyan">{m("alert_event_recall_warning")}</Stat>
            <Stat label="Median lead time" note="negative: after the event started">{m("alert_median_lead_time_min", "min")}</Stat>
            <Stat label="Median decision window" note="first alert to the event's recorded end" accent="cyan">{m("alert_median_decision_window_min", "dur")}</Stat>
            <Stat label="False alerts">{m("alert_false_per_24h", "per24h")}</Stat>
            <Stat label="Brier skill vs prior">{m("alert_brier_skill", "num")}</Stat>
          </CardBody>
        </Card>
        {replayWells.length === 0 ? (
          <Notice tone="advisory" title="No replay yet">
            Run <span className="num">make evaluation</span> to replay every well; this page shows the result.
          </Notice>
        ) : (
          <>
            <Field label="Held-out well">
              <NativeSelect value={well} onChange={(e) => setWell(e.target.value)} className="max-w-md" data-testid="replay-well">
                {replayWells.map((w) => (
                  <option key={w.well} value={w.well}>
                    {w.well} · {w.events.length} event{w.events.length === 1 ? "" : "s"} · {w.false_alerts} false alert{w.false_alerts === 1 ? "" : "s"}
                  </option>
                ))}
              </NativeSelect>
            </Field>
            <Studio key={well} well={well} />
          </>
        )}
        <Card>
          <CardHeader title="Extraction and chat" />
          <CardBody className="flex flex-wrap gap-6">
            {["typed", "scanned", "handwritten", "fax"].map((k) => (
              <Stat key={k} label={`${k} documents F1`} accent="sepia">
                {m(`extraction_${k}_f1`)}
              </Stat>
            ))}
            <Stat label="Chat answers correct" accent="violet">{m("chat_rules_accuracy")}</Stat>
            <Stat label="Chat answers cited" accent="violet">{m("chat_rules_citation")}</Stat>
          </CardBody>
        </Card>
      </div>
    </>
  );
}

