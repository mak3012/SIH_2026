import {
  Button,
  Card,
  CardBody,
  CardHeader,
  DataTable,
  EmptyState,
  Field,
  FormationSwatch,
  Input,
  NativeSelect,
  Notice,
  Quantity,
  RiskTierBadge,
  RulesAIChip,
  Skeleton,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  tierOf,
} from "@repo/ui";
import { BellOff, Download, TriangleAlert } from "lucide-react";
import { lazy, Suspense, useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { can } from "../auth";
import { CachedBanner, DataState, ErrorNote, NeedWell, PageHeader, pct, SignInGate, useLoad, words } from "../components/common";
import type { Correlation } from "../components/CorrelationPanel";
import { Evidence, type Citation } from "../components/EvidenceViewer";
import { HazardStrip } from "../components/HazardStrip";
import { href } from "../router";
import type { SceneData } from "../components/Scene3D";
import { savedAt, savePackage } from "../offline";
import { useApp } from "../state";

const Scene3D = lazy(() => import("../components/Scene3D"));
const CorrelationPanel = lazy(() => import("../components/CorrelationPanel").then((m) => ({ default: m.CorrelationPanel })));
const VIEWS = ["roadmap", "correlation", "3d", "alerts", "offsets"];

type Cell = { formation: string; hazard: string; mean: number; ci90: [number, number]; n_eff: number; counts: Record<string, number>; evidence_state: string; band_md: [number, number] | null };
type Profile = { selection_id: number; offsets: { well: string; weight: number }[]; formations: { formation: string; top_md: number | null; base_md: number | null; top_tvdss: number | null }[]; cells: Cell[] };
type Band = { formation: string; hazard: string; mean: number; band_md: [number, number] | null; state: string; distance_m: number | null; eta_hours: number | null };
type Look = { current_md: number | null; rate_m_per_hr: number | null; rate_basis: string; bands: Band[] };
type Alert = { id: number; hazard: string; tier: number; posterior: number; raised_at: string; cleared_at: string | null; status: string; ack_by: string | null; explanation: string | null; citations: Citation[] | null; formation: string | null };

const HAZARDS = ["loss", "kick", "stuck_pipe", "tight_hole", "pack_off", "washout"];

function Roadmap({ p }: { p: Profile }) {
  const forms = p.formations.map((f) => f.formation);
  const hz = HAZARDS.filter((h) => p.cells.some((c) => c.hazard === h));
  const cell = (f: string, h: string) => p.cells.find((c) => c.formation === f && c.hazard === h);
  return (
    <Card>
      <CardHeader
        title="Hazard roadmap"
        description={`Chance of each hazard somewhere in the formation, from ${p.offsets.length} offset wells (weighted). Hatched: no offset evidence, prior only.`}
        actions={<RulesAIChip ai={false} className="shrink-0" />}
      />
      <CardBody className="overflow-x-auto">
        <table className="w-full border-collapse text-sm">
          <caption className="sr-only">Hazard probability by formation</caption>
          <thead>
            <tr>
              <th scope="col" className="px-2 py-2 text-left text-xs uppercase text-muted">Formation</th>
              {hz.map((h) => (
                <th key={h} scope="col" className="px-2 py-2 text-left text-xs uppercase text-muted">{words(h)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {forms.map((f, i) => (
              <tr key={f} className="border-t border-line">
                <th scope="row" className="px-2 py-2 text-left font-medium">
                  <FormationSwatch name={f} index={i} />
                </th>
                {hz.map((h) => {
                  const c = cell(f, h);
                  if (!c) return <td key={h} className="px-2 text-muted">–</td>;
                  const prior = c.evidence_state === "no_evidence";
                  return (
                    <td key={h} className="px-1 py-1" data-testid={`cell-${f}-${h}`}>
                      <div
                        className="rounded-md px-2 py-1.5"
                        style={{
                          background: prior ? "repeating-linear-gradient(45deg, transparent 0 5px, var(--border) 5px 6px)" : `color-mix(in srgb, var(--petrol-solid) ${Math.round(Math.min(0.7, c.mean) * 70)}%, transparent)`,
                          color: !prior && c.mean > 0.4 ? "#fff" : undefined,
                        }}
                      >
                        <span className="num font-semibold">{pct(c.mean)}</span>
                        {c.mean >= 0.3 && <TriangleAlert aria-label="high" className="ml-1 inline size-3.5" />}
                        <div className="num text-xs opacity-90">
                          {pct(c.ci90[0])}–{pct(c.ci90[1])} · n<sub>eff</sub> {c.n_eff.toFixed(1)}
                        </div>
                        <div className="text-[11px] opacity-90">
                          {c.counts.event} event · {c.counts.none} none · {c.counts.unknown} unknown
                        </div>
                      </div>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </CardBody>
    </Card>
  );
}

function Lookahead({ l }: { l: Look }) {
  const ahead = l.bands.filter((b) => b.state !== "passed").slice(0, 8);
  return (
    <Card>
      <CardHeader title="Lookahead" description={<>Bit at <Quantity value={l.current_md} unit="m MD" digits={0} />, net progress <Quantity value={l.rate_m_per_hr} unit="m/h" /> ({words(l.rate_basis ?? "–")}).</>} />
      <CardBody className="p-0">
        <DataTable
          caption="Hazard bands ahead of the bit"
          head={["Band", "Probability", "Depth", "State", "Distance", "ETA"]}
          rows={ahead.map((b) => [
            `${b.formation} · ${words(b.hazard)}`,
            <span key="p" className="num">{pct(b.mean)}</span>,
            b.band_md ? <span key="d" className="num">{b.band_md[0].toFixed(0)}–{b.band_md[1].toFixed(0)} m MD</span> : "–",
            b.state === "in band" ? <RiskTierBadge key="s" tier="warning">{" in band"}</RiskTierBadge> : <span key="s">{b.state}</span>,
            <Quantity key="dist" value={b.distance_m} unit="m" digits={0} />,
            <Quantity key="eta" value={b.eta_hours} unit="h" />,
          ])}
        />
      </CardBody>
    </Card>
  );
}

export function AlertList({ id }: { id: number }) {
  const { user } = useApp();
  const { data, error, reload, cachedAt } = useLoad<Alert[]>(`/active/${id}/alerts`);
  const [shelf, setShelf] = useState({ hazard: "kick", reason: "", minutes: 60 });
  const [err, setErr] = useState<unknown>(null);
  const write = can(user, "operate");
  return (
    <div className="grid gap-4">
      <CachedBanner at={cachedAt} />
      <ErrorNote error={error ?? err} />
      <Card>
        <CardHeader title="Alerts" description="Open alerts and the last 12 hours. Rules raise them; AI may add the explanation." />
        <CardBody className="grid gap-3">
          {data && data.length === 0 && <EmptyState title="No alerts" />}
          {(data ?? []).map((a) => (
            <article key={a.id} className="rounded-md border border-line p-3">
              <div className="flex flex-wrap items-center gap-3">
                <RiskTierBadge tier={tierOf(a.tier)} />
                <strong>{words(a.hazard)}</strong>
                <span className="num">{pct(a.posterior)}</span>
                <RulesAIChip ai={!!a.explanation} />
                <span className="text-sm text-muted">
                  {new Date(a.raised_at).toLocaleString()} · {a.status}
                  {a.ack_by ? ` by ${a.ack_by}` : ""}
                  {a.cleared_at ? " · back to normal" : ""}
                </span>
                {a.status === "open" && write && (
                  <Button size="sm" className="ml-auto" onClick={() => api(`/alerts/${a.id}/ack`, { method: "POST" }).then(reload, setErr)}>
                    Acknowledge
                  </Button>
                )}
              </div>
              {a.explanation && <p className="mb-0 mt-2 text-sm">{a.explanation}</p>}
              {a.citations && a.citations.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-2">
                  {a.citations.map((c) => (
                    <Evidence key={c.id} c={{ ...c, type: c.type ?? "document" }} />
                  ))}
                </div>
              )}
            </article>
          ))}
        </CardBody>
      </Card>
      {write && (
        <Card>
          <CardHeader title="Shelve a hazard" description="Advisory and warning alerts are recorded but not delivered. Critical alerts are never shelved; at most one shift." />
          <CardBody>
            <form
              className="grid gap-3 md:grid-cols-[200px_140px_1fr_auto] md:items-end"
              onSubmit={(e) => {
                e.preventDefault();
                api(`/active/${id}/shelves`, { json: shelf }).then(() => setShelf({ ...shelf, reason: "" }), setErr);
              }}
            >
              <Field label="Hazard">
                <NativeSelect value={shelf.hazard} onChange={(e) => setShelf({ ...shelf, hazard: e.target.value })}>
                  {HAZARDS.map((h) => <option key={h} value={h}>{words(h)}</option>)}
                </NativeSelect>
              </Field>
              <Field label="Minutes">
                <Input type="number" min={1} max={720} value={shelf.minutes} onChange={(e) => setShelf({ ...shelf, minutes: Number(e.target.value) })} />
              </Field>
              <Field label="Reason (required)">
                <Input required minLength={5} value={shelf.reason} onChange={(e) => setShelf({ ...shelf, reason: e.target.value })} placeholder="Pit transfer in progress" />
              </Field>
              <Button type="submit" variant="secondary">
                <BellOff /> Shelve
              </Button>
            </form>
          </CardBody>
        </Card>
      )}
    </div>
  );
}

function SelectOffsets({ id, onDone }: { id: number; onDone: () => void }) {
  const { user } = useApp();
  const forms = useLoad<{ formation: string; top_md: number | null }[]>(`/wellbores/${id}/formations`);
  const [target, setTarget] = useState("");
  const [radii, setRadii] = useState({ surface: 10000, sub: 5000 });
  const [result, setResult] = useState<{ candidates: { well: string; score: number; included: boolean; formation_m: number | null; surface_m: number }[] } | null>(null);
  const [err, setErr] = useState<unknown>(null);
  useEffect(() => {
    const named = (forms.data ?? []).filter((f) => f.top_md != null);
    if (!target && named.length) setTarget(named[Math.floor(named.length / 2)].formation);
  }, [forms.data, target]);
  return (
    <Card>
      <CardHeader title="Select offset wells" description="Rank analogue wells at a target formation: distance where trajectories cross it, fault block, depth, design, mud, vintage, data quality." />
      <CardBody className="grid gap-4">
        <form
          className="grid gap-3 md:grid-cols-[1fr_160px_160px_auto] md:items-end"
          onSubmit={(e) => {
            e.preventDefault();
            api<typeof result>("/offsets/select", { json: { active_wellbore_id: id, formation: target, surface_radius_m: radii.surface, subsurface_radius_m: radii.sub } }).then((r) => {
              setResult(r);
              onDone();
            }, setErr);
          }}
        >
          <Field label="Target formation">
            <NativeSelect value={target} onChange={(e) => setTarget(e.target.value)}>
              {(forms.data ?? []).map((f) => <option key={f.formation}>{f.formation}</option>)}
            </NativeSelect>
          </Field>
          <Field label="Surface radius (m)">
            <Input type="number" min={500} max={100000} value={radii.surface} onChange={(e) => setRadii({ ...radii, surface: Number(e.target.value) })} />
          </Field>
          <Field label="At formation (m)">
            <Input type="number" min={500} max={100000} value={radii.sub} onChange={(e) => setRadii({ ...radii, sub: Number(e.target.value) })} />
          </Field>
          <Button type="submit" disabled={!target || !can(user, "plan")} title={can(user, "plan") ? undefined : "needs the planner role"}>
            Select offsets
          </Button>
        </form>
        <ErrorNote error={err} />
        {result && (
          <DataTable
            caption="Ranked candidates"
            head={["Well", "Score", "At formation", "Surface", "Included"]}
            rows={result.candidates.map((c) => [
              <span key="w" className="font-mono">{c.well}</span>,
              <span key="s" className="num">{c.score.toFixed(3)}</span>,
              <Quantity key="f" value={c.formation_m} unit="m" digits={0} />,
              <Quantity key="su" value={c.surface_m} unit="m" digits={0} />,
              c.included ? "yes" : "no",
            ])}
          />
        )}
      </CardBody>
    </Card>
  );
}

function CorrelationTab({ id }: { id: number }) {
  const corr = useLoad<Correlation>(`/correlation/${id}`);
  return (
    <DataState state={corr} skeleton="h-[720px]" isEmpty={(d) => d.tracks.length < 2} empty={{ title: "No offset wells to correlate", body: "Select offsets first (Offsets tab or the offset map)." }}>
      {(d) => (
        <div className="grid gap-4">
          <Card>
            <CardHeader title="Correlation" description={`${d.tracks[0].well} and ${d.tracks.length - 1} offsets, in TVDSS, flattened on a formation top. This well shows its prognosed tops, the roadmap's hazard bands and the bit.`} />
            <CardBody>
              <Suspense fallback={<Skeleton className="h-[720px] w-full" />}>
                <CorrelationPanel data={d} />
              </Suspense>
            </CardBody>
          </Card>
          <details className="rounded-md border border-line bg-surface p-3">
            <summary className="cursor-pointer font-medium">Events as a table</summary>
            <DataTable
              caption="Offset events in the correlation"
              head={["Well", "Event", "Depth", "Formation", "MW / ECD", "Source"]}
              rows={d.tracks.flatMap((t) =>
                t.events.map((e) => [
                  <span key="w" className="num">{t.well}</span>,
                  `${words(e.type)}${e.severity ? ` (S${e.severity})` : ""}`,
                  <Quantity key="d" value={e.md} unit="m MD" digits={0} />,
                  e.formation ?? "–",
                  <span key="m" className="num">{e.mw ? `${e.mw.toFixed(2)} sg` : "–"}{e.ecd ? ` / ${e.ecd.toFixed(2)} sg` : ""}</span>,
                  e.document_id && e.page ? <Evidence key="s" c={{ id: `EV${e.id}`, type: "document", document: e.document ?? "", document_id: e.document_id, page: e.page, bbox: e.bbox, review_status: e.review_status }} /> : "–",
                ]),
              )}
            />
          </details>
        </div>
      )}
    </DataState>
  );
}

function WellBody({ id, view }: { id: number; view: string | null }) {
  const { wells, select, selected, live } = useApp();
  const w = wells?.find((x) => x.wellbore_id === id);
  useEffect(() => {
    if (selected !== id) select(id);
  }, [id, selected, select]);
  const profile = useLoad<Profile>(`/active/${id}/hazard-profile`);
  const look = useLoad<Look>(profile.data ? `/active/${id}/lookahead` : null);
  const tab = view && VIEWS.includes(view) ? view : "roadmap";
  const setTab = (v: string) => {
    location.hash = href("well", id, v === "roadmap" ? null : v);
  };
  const scene = useLoad<SceneData>(tab === "3d" ? `/wellbores/${id}/scene` : null);
  const [saved, setSaved] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    savedAt(id).then(setSaved);
  }, [id]);
  const needsOffsets = profile.error instanceof ApiError && profile.error.status === 409;
  return (
    <>
      <PageHeader
        title={w?.wellbore ?? `Wellbore ${id}`}
        badge={<span className="num text-base font-normal text-muted">{w?.block} · TD <Quantity value={w?.td_md} unit="m MD" digits={0} /></span>}
        description="Hazard roadmap from offset wells, lookahead from the bit, the subsurface in 3D, and alerts."
        actions={
          <Button
            variant="secondary"
            disabled={saving}
            onClick={() => {
              setSaving(true);
              savePackage(id).then(() => savedAt(id).then(setSaved)).finally(() => setSaving(false));
            }}
          >
            <Download /> {saving ? "Saving…" : "Save for offline"}
          </Button>
        }
      />
      {saved && <p className="-mt-3 mb-4 text-sm text-muted">Hazard package saved for offline use <span className="num">{new Date(saved).toLocaleString()}</span>.</p>}
      <CachedBanner at={profile.cachedAt} />
      <Tabs value={tab} onValueChange={setTab}>
        <TabsList aria-label="Well views" className="mb-4">
          <TabsTrigger value="roadmap">Roadmap</TabsTrigger>
          <TabsTrigger value="correlation">Correlation</TabsTrigger>
          <TabsTrigger value="3d">3D subsurface</TabsTrigger>
          <TabsTrigger value="alerts">Alerts</TabsTrigger>
          <TabsTrigger value="offsets">Offsets</TabsTrigger>
        </TabsList>
        <TabsContent value="roadmap" className="grid gap-4">
          {profile.loading && !profile.data && <Skeleton className="h-[720px] w-full" />}
          {needsOffsets && <Notice tone="advisory" title="No offset wells selected yet">The roadmap needs analogue wells: select them in the Offsets tab or on the offset map.</Notice>}
          {!needsOffsets && <ErrorNote error={profile.error} onRetry={profile.reload} />}
          {profile.data && look.data && (
            <Card>
              <CardHeader
                title="Hazard roadmap along the well"
                description="Per formation and hazard: probability (bar), 90 % interval (whisker), effective number of offset wells (n). Drawn where the offsets had it; hatched = prior only. The next band ahead is outlined in gold."
                actions={<RulesAIChip ai={false} className="shrink-0" />}
              />
              <CardBody>
                <HazardStrip
                  formations={profile.data.formations}
                  cells={profile.data.cells}
                  bands={look.data.bands}
                  bitMd={look.data.current_md}
                  rollingRate={look.data.rate_m_per_hr}
                  rollingBasis={look.data.rate_basis}
                  live={{ rop: live.sample?.values.rop, bit_md: live.sample?.values.bit_md, ts: live.sample?.ts, fresh: live.feed === "live" }}
                />
              </CardBody>
            </Card>
          )}
          {profile.data && <Roadmap p={profile.data} />}
          {look.data && <Lookahead l={look.data} />}
        </TabsContent>
        <TabsContent value="correlation">{tab === "correlation" && <CorrelationTab id={id} />}</TabsContent>
        <TabsContent value="3d">
          {scene.loading && <Skeleton className="h-[560px] w-full" />}
          <ErrorNote error={scene.error} />
          {scene.data && (
            <Suspense fallback={<Skeleton className="h-[560px] w-full" />}>
              <Scene3D data={scene.data} />
              <Card className="mt-4">
                <CardHeader title="In the scene" />
                <CardBody className="p-0">
                  <DataTable
                    caption="Wells in the 3D scene"
                    head={["Well", "Role", "Tops", "Events"]}
                    rows={scene.data.wells.map((x) => [
                      <span key="w" className="font-mono">{x.well}</span>,
                      x.active ? "this well" : "offset",
                      x.tops.map((t) => t.formation).join(", ") || "–",
                      x.events.map((e) => `${words(e.type)} ${e.md.toFixed(0)} m`).join("; ") || "none",
                    ])}
                  />
                </CardBody>
              </Card>
            </Suspense>
          )}
        </TabsContent>
        <TabsContent value="alerts">
          <AlertList id={id} />
        </TabsContent>
        <TabsContent value="offsets" className="grid gap-4">
          {profile.data && (
            <Card>
              <CardHeader title="Current offsets" />
              <CardBody>
                <ul className="m-0 flex list-none flex-wrap gap-2 p-0">
                  {profile.data.offsets.map((o) => (
                    <li key={o.well} className="rounded-md border border-line px-3 py-1 font-mono text-sm">
                      {o.well} <span className="text-muted">w {o.weight.toFixed(2)}</span>
                    </li>
                  ))}
                </ul>
              </CardBody>
            </Card>
          )}
          <SelectOffsets id={id} onDone={profile.reload} />
        </TabsContent>
      </Tabs>
    </>
  );
}

export function WellPage({ id, view }: { id: number | null; view?: string | null }) {
  const { selected } = useApp();
  return (
    <SignInGate>
      <NeedWell id={id ?? selected}>{(i) => <WellBody id={i} view={view ?? null} />}</NeedWell>
    </SignInGate>
  );
}
