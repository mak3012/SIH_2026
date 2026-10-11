import { Button, Card, CardBody, CardHeader, cn, Field, NativeSelect, Notice, Quantity, Skeleton } from "@repo/ui";
import { Check, ExternalLink, Layers3, MapPinned, Radio } from "lucide-react";
import { lazy, Suspense, useEffect, useMemo, useState, type ReactNode } from "react";
import { api } from "../api";
import { can } from "../auth";
import { DataState, ErrorNote, PageHeader, SignInGate, useLoad, words } from "../components/common";
import type { Layers, MapWell, PublicWell, Region } from "../components/OffsetMapView";
import { href } from "../router";
import { useApp, useSelectedWell, type Wellbore } from "../state";

const OffsetMapView = lazy(() => import("../components/OffsetMapView"));

type Preview = { wellbore_id: number; formation: string; surface_radius_m: number; subsurface_radius_m: number; weights: Record<string, number>; wells: MapWell[] };

/** Debounce a value (sliders refetch only when the hand stops). */
function useSettled<T>(v: T, ms = 350): T {
  const [s, setS] = useState(v);
  useEffect(() => {
    const t = setTimeout(() => setS(v), ms);
    return () => clearTimeout(t);
  }, [v, ms]);
  return s;
}

function FactorBars({ w }: { w: MapWell }) {
  const c = w.candidate!;
  return (
    <table className="mt-2 w-full border-collapse text-xs">
      <caption className="sr-only">Analogue factors for {w.well}</caption>
      <thead>
        <tr className="text-left text-muted">
          <th scope="col" className="py-1 font-medium">Factor</th>
          <th scope="col" className="py-1 font-medium">Score</th>
          <th scope="col" className="py-1 text-right font-medium">Weight</th>
        </tr>
      </thead>
      <tbody>
        {c.factors.map((f) => (
          <tr key={f.name} title={f.why}>
            <th scope="row" className="w-28 py-0.5 text-left font-normal">{words(f.name)}</th>
            <td className="py-0.5">
              <div className="flex items-center gap-2">
                <div className="h-2.5 flex-1 rounded-sm bg-surface-2" aria-hidden>
                  <div
                    className={cn("h-2.5 rounded-sm", f.unknown ? "bg-[repeating-linear-gradient(45deg,var(--muted)_0_3px,transparent_3px_6px)]" : "bg-petrol-solid")}
                    style={{ width: `${Math.round((f.unknown ? 1 : f.score) * 100)}%` }}
                  />
                </div>
                <span className="num w-10 text-right">{f.unknown ? "n/a" : f.score.toFixed(2)}</span>
              </div>
            </td>
            <td className="num py-0.5 text-right text-muted">{f.weight.toFixed(2)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Ranked({ wells, focus, onFocus }: { wells: MapWell[]; focus: number | null; onFocus: (id: number) => void }) {
  const ranked = wells.filter((w) => w.candidate).sort((a, b) => a.candidate!.rank - b.candidate!.rank);
  if (!ranked.length) return <p className="m-0 p-4 text-muted">No candidate wells inside the surface radius. Widen it.</p>;
  return (
    <ol className="m-0 list-none p-0" aria-label="Analogue wells by score">
      {ranked.map((w) => {
        const c = w.candidate!;
        const open = w.well_id === focus;
        return (
          <li key={w.well_id} className="border-b border-line">
            <button
              type="button"
              onClick={() => onFocus(w.well_id)}
              aria-expanded={open}
              data-testid={`analogue-${w.well}`}
              className={cn("flex min-h-target w-full items-center gap-3 px-4 py-2 text-left hover:bg-surface-2", open && "bg-teal-subtle")}
            >
              <span className="num w-6 text-right text-sm text-muted">{c.rank}</span>
              <span className="min-w-0 flex-1">
                <span className="num block font-semibold">{w.well}</span>
                <span className="block text-xs text-muted">
                  <Quantity value={c.surface_m / 1000} unit="km" digits={1} /> surface ·{" "}
                  {c.formation_m == null ? "at formation n/a" : <><Quantity value={c.formation_m / 1000} unit="km" digits={1} /> at formation</>}
                </span>
              </span>
              <span className="w-24">
                <span className="block h-2 rounded-sm bg-surface-2" aria-hidden>
                  <span className="block h-2 rounded-sm bg-petrol-solid" style={{ width: `${Math.round(c.score * 100)}%` }} />
                </span>
                <span className="num block text-right text-sm">{c.score.toFixed(2)}</span>
              </span>
              {c.included ? (
                <span className="inline-flex items-center gap-1 text-xs font-semibold text-petrol"><Check aria-hidden className="size-3.5" />offset</span>
              ) : (
                <span className="text-xs text-muted">{c.within_radii ? "below cut" : "outside"}</span>
              )}
            </button>
            {open && (
              <div className="px-4 pb-3">
                <FactorBars w={w} />
              </div>
            )}
          </li>
        );
      })}
    </ol>
  );
}

function Legend({ layers, set }: { layers: Layers; set: (l: Layers) => void }) {
  const items: [keyof Layers, string, string][] = [
    ["trajectories", "3D trajectories", "bg-petrol-solid"],
    ["radii", "Surface and at-formation radius", "bg-teal-solid"],
    ["halos", "Position uncertainty (datum), gold ring", "bg-gold-solid"],
    ["heat", "Evidence density", "bg-sepia-solid"],
    ["faults", "Fault block of each well", "bg-muted"],
  ];
  return (
    <fieldset className="m-0 flex flex-wrap gap-x-4 gap-y-1 border-0 p-0">
      <legend className="sr-only">Map layers</legend>
      {items.map(([k, label, sw]) => (
        <label key={k} className="inline-flex min-h-target items-center gap-2 text-sm">
          <input type="checkbox" checked={layers[k]} onChange={(e) => set({ ...layers, [k]: e.target.checked })} className="size-4 accent-[var(--teal-solid)]" />
          <span aria-hidden className={cn("inline-block h-2.5 w-5 rounded-sm", sw)} />
          {label}
        </label>
      ))}
    </fieldset>
  );
}

type SyncStatus = { wells: number; last_sync: string | null; newest_spud: string | null; new_days: number };

/** Public wells, refreshed every 5 minutes while the page is open (the sync itself is daily). */
function usePublicWells() {
  const wells = useLoad<PublicWell[]>("/public-wells");
  const status = useLoad<SyncStatus>("/public-wells/status");
  const { reload: r1 } = wells;
  const { reload: r2 } = status;
  useEffect(() => {
    const t = setInterval(() => {
      r1();
      r2();
    }, 5 * 60_000);
    return () => clearInterval(t);
  }, [r1, r2]);
  return { wells, status };
}

const day = (iso: string | null) => (iso ? new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "–");

function PublicWellCard({ w, onClose }: { w: PublicWell; onClose: () => void }) {
  const rows: [string, ReactNode][] = [
    ["Area", w.area],
    ["Purpose", w.purpose],
    ["Status", w.status],
    ["Content", w.content],
    ["Operator", w.operator],
    ["Field", w.field],
    ["Spudded", day(w.entry_date)],
    ["Completed", day(w.completion_date)],
    ["Total depth", w.total_depth_m != null ? <Quantity value={w.total_depth_m} unit="m MD" digits={0} /> : "–"],
    ["Water depth", w.water_depth_m != null ? <Quantity value={w.water_depth_m} unit="m" digits={0} /> : "–"],
    ["Formation at TD", w.formation_at_td],
    ["Reports ingested", w.reports],
  ];
  return (
    <Card data-testid="public-well">
      <CardHeader
        title={`${w.name}${w.is_new ? " · new" : ""}`}
        description={`Sodir FactPages, first seen here ${day(w.first_seen)}`}
        actions={
          <Button variant="secondary" size="sm" onClick={onClose}>
            Close
          </Button>
        }
      />
      <CardBody className="grid gap-3">
        <dl className="m-0 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
          {rows.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="text-muted">{k}</dt>
              <dd className="m-0">{v || "–"}</dd>
            </div>
          ))}
        </dl>
        <div className="flex flex-wrap gap-3 text-sm">
          {w.factpage_url && (
            <a href={w.factpage_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1">
              FactPage <ExternalLink aria-hidden className="size-3.5" />
            </a>
          )}
          {w.press_release_url && (
            <a href={w.press_release_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1">
              Press release <ExternalLink aria-hidden className="size-3.5" />
            </a>
          )}
        </div>
      </CardBody>
    </Card>
  );
}

function Newest({ wells, onPick }: { wells: PublicWell[]; onPick: (w: PublicWell) => void }) {
  const newest = wells
    .filter((w) => w.entry_date)
    .toSorted((a, b) => b.entry_date!.localeCompare(a.entry_date!))
    .slice(0, 12);
  return (
    <ol className="m-0 list-none p-0" aria-label="Newest public wells">
      {newest.map((w) => (
        <li key={w.id} className="border-b border-line">
          <button type="button" onClick={() => onPick(w)} className="flex min-h-target w-full items-center gap-3 px-4 py-2 text-left hover:bg-surface-2">
            <span className="min-w-0 flex-1">
              <span className="num block font-semibold">
                {w.name} {w.is_new && <span className="rounded bg-gold-solid px-1 text-xs font-semibold text-black">NEW</span>}
              </span>
              <span className="block truncate text-xs text-muted">{[w.purpose, w.status, w.operator].filter(Boolean).join(" · ")}</span>
            </span>
            <span className="text-xs text-muted">{day(w.entry_date)}</span>
          </button>
        </li>
      ))}
    </ol>
  );
}

function AllWells({ wells }: { wells: Wellbore[] }) {
  const { wells: pub, status } = usePublicWells();
  const [picked, setPicked] = useState<PublicWell | null>(null);
  const flyTo = useMemo(() => (picked ? ([picked.lon, picked.lat] as [number, number]) : null), [picked]);
  const publicWells = pub.data ?? [];
  const regions: Region[] = useMemo(() => {
    const own = wells.filter((w) => w.lat != null).map((w) => [w.lon!, w.lat!] as [number, number]);
    const reg = publicWells.map((w) => [w.lon, w.lat] as [number, number]);
    return [
      { label: "Upper Assam", points: own },
      ...(reg.length ? [{ label: "Norwegian shelf", points: reg }, { label: "All", points: [...own, ...reg] }] : []),
    ];
  }, [wells, publicWells]);
  const s = status.data;
  const fresh = publicWells.filter((w) => w.is_new).length;
  return (
    <div className="grid gap-4 xl:grid-cols-[1fr_380px]">
      <Card className="h-[640px] overflow-hidden">
        <WellsOnly wells={wells} publicWells={publicWells} regions={regions} onPickPublic={setPicked} flyTo={flyTo} />
      </Card>
      <div className="grid content-start gap-4">
        {picked && <PublicWellCard w={picked} onClose={() => setPicked(null)} />}
        <Card>
          <CardHeader
            title="Public wells, live"
            description={
              s && s.wells
                ? `${s.wells.toLocaleString()} Sodir wellbores, ${fresh} new in ${s.new_days} days. Synced ${s.last_sync ? new Date(s.last_sync).toLocaleString() : "never"}; checked daily.`
                : "No public wells yet: the daily Sodir sync has not run (docs/guide.md)."
            }
            actions={<Radio aria-hidden className="size-5 text-petrol" />}
          />
          <div className="max-h-[440px] overflow-auto" aria-busy={pub.loading}>
            {publicWells.length ? <Newest wells={publicWells} onPick={setPicked} /> : null}
          </div>
          <p className="m-0 px-4 py-2 text-xs text-muted">Contains data from the Norwegian Offshore Directorate (Sodir), NLOD 2.0. The 30 Upper Assam wells are synthetic.</p>
        </Card>
      </div>
    </div>
  );
}

function WellsOnly({
  wells,
  publicWells,
  regions,
  onPickPublic,
  flyTo,
}: {
  wells: Wellbore[];
  publicWells?: PublicWell[];
  regions?: Region[];
  onPickPublic?: (w: PublicWell) => void;
  flyTo?: [number, number] | null;
}) {
  const mapped: MapWell[] = wells
    .filter((w) => w.lat != null && w.lon != null)
    .map((w) => ({
      well_id: w.wellbore_id,
      well: w.wellbore,
      wellbore_id: w.wellbore_id,
      active: false,
      block: w.block,
      fault_block: null,
      lon: w.lon!,
      lat: w.lat!,
      datum_confidence: null,
      position_uncertainty_m: null,
      events: 0,
      evidence: 0,
      path: [],
      at_formation: null,
      candidate: null,
    }));
  const { select } = useApp();
  return (
    <Suspense fallback={<Skeleton className="h-[560px] w-full" />}>
      <OffsetMapView
        wells={mapped}
        surfaceRadius={null}
        subsurfaceRadius={null}
        exaggeration={1}
        layers={{ trajectories: false, halos: false, heat: false, faults: false, radii: false }}
        focus={null}
        onPick={(id) => {
          select(id);
          location.hash = href("map");
        }}
        publicWells={publicWells}
        regions={regions}
        onPickPublic={onPickPublic}
        flyTo={flyTo}
        earth
      />
    </Suspense>
  );
}

function OffsetMap({ id }: { id: number }) {
  const { user } = useApp();
  const forms = useLoad<{ formation: string; top_md: number | null }[]>(`/wellbores/${id}/formations`);
  const [formation, setFormation] = useState<string | null>(null);
  const [surface, setSurface] = useState(15000);
  const [sub, setSub] = useState(8000);
  const [exag, setExag] = useState(1);
  const [layers, setLayers] = useState<Layers>({ trajectories: true, halos: true, heat: true, faults: true, radii: true });
  const [focus, setFocus] = useState<number | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const target = formation ?? forms.data?.filter((f) => f.top_md != null).at(-2)?.formation ?? null;
  const r = useSettled({ surface, sub, target });
  const preview = useLoad<Preview>(
    r.target ? `/offsets/preview/${id}?formation=${encodeURIComponent(r.target)}&surface_radius_m=${r.surface}&subsurface_radius_m=${r.sub}` : null,
  );
  // What the user sees selected: their choice, else the server's name for the default.
  const shown = formation ?? preview.data?.formation ?? target;

  async function choose() {
    setErr(null);
    try {
      const res = await api<{ selection_id: number }>("/offsets/select", {
        json: { active_wellbore_id: id, formation: shown, surface_radius_m: surface, subsurface_radius_m: sub },
      });
      setSaved(`Selection ${res.selection_id} saved.`);
    } catch (e) {
      setErr(e);
    }
  }

  const included = preview.data?.wells.filter((w) => w.candidate?.included).length ?? 0;
  return (
    <div className="grid gap-4">
      <Card>
        <CardBody className="grid gap-3 lg:grid-cols-[220px_1fr_1fr_160px_auto] lg:items-end">
          <Field label="Target formation">
            <NativeSelect value={shown ?? ""} onChange={(e) => setFormation(e.target.value)} disabled={!forms.data}>
              {(forms.data ?? []).map((f) => (
                <option key={f.formation}>{f.formation}</option>
              ))}
            </NativeSelect>
          </Field>
          <label className="grid gap-1 text-sm">
            <span className="font-medium">
              Surface radius <Quantity value={surface / 1000} unit="km" digits={1} />
            </span>
            <input type="range" min={1000} max={40000} step={500} value={surface} onChange={(e) => setSurface(Number(e.target.value))} className="min-h-target accent-[var(--teal-solid)]" />
          </label>
          <label className="grid gap-1 text-sm">
            <span className="font-medium">
              At-formation radius <Quantity value={sub / 1000} unit="km" digits={1} />
            </span>
            <input type="range" min={500} max={30000} step={500} value={sub} onChange={(e) => setSub(Number(e.target.value))} className="min-h-target accent-[var(--petrol-solid)]" />
          </label>
          <label className="grid gap-1 text-sm">
            <span className="font-medium">
              Vertical ×<span className="num">{exag}</span>
            </span>
            <input type="range" min={1} max={6} step={1} value={exag} onChange={(e) => setExag(Number(e.target.value))} className="min-h-target" />
          </label>
          <Button onClick={choose} disabled={!can(user, "plan") || !included} title={can(user, "plan") ? undefined : "needs the planner role"}>
            Use these {included} offsets
          </Button>
          <div className="lg:col-span-5">
            <Legend layers={layers} set={setLayers} />
          </div>
        </CardBody>
      </Card>
      {saved && (
        <Notice tone="advisory" title={saved}>
          The roadmap now uses them: <a href={href("well", id)}>open the well</a>.
        </Notice>
      )}
      <ErrorNote error={err} />
      <DataState
        state={preview}
        skeleton="h-[600px]"
        isEmpty={(d) => d.wells.length === 0}
        empty={{ title: "No wells to map", body: "None of the wells you can access has a surface location.", icon: <MapPinned /> }}
      >
        {(d) => (
          <div className="grid gap-4 xl:grid-cols-[1fr_400px]">
            <Card className="overflow-hidden">
              <div className="h-[640px]">
                <Suspense fallback={<Skeleton className="h-full w-full" />}>
                  <OffsetMapView wells={d.wells} surfaceRadius={d.surface_radius_m} subsurfaceRadius={d.subsurface_radius_m} exaggeration={exag} layers={layers} focus={focus} onPick={setFocus} />
                </Suspense>
              </div>
            </Card>
            <Card>
              <CardHeader
                title="Analogue scores"
                description={`At the ${d.formation} top. Score = Σ weight × factor; bars per factor. ${preview.loading ? "Updating…" : ""}`}
              />
              <div className="max-h-[560px] overflow-auto" aria-busy={preview.loading}>
                <Ranked wells={d.wells} focus={focus} onFocus={(w) => setFocus(focus === w ? null : w)} />
              </div>
            </Card>
          </div>
        )}
      </DataState>
    </div>
  );
}

export function MapPage() {
  const { wells, wellsError, selected } = useApp();
  const w = useSelectedWell();
  const ok = useMemo(() => (wells ?? []).filter((x) => x.lat != null), [wells]);
  const [view, setView] = useState<"all" | "offsets">(selected != null ? "offsets" : "all");
  const showOffsets = view === "offsets" && selected != null;
  return (
    <>
      <PageHeader
        title={showOffsets && w ? `Offset map · ${w.wellbore}` : "Wells map"}
        description={
          showOffsets
            ? "Wells, their 3D trajectories and where they cross the target top; surface and at-formation search radii; how sure each surface position is; where the records are dense. The list ranks analogues by score, factor by factor."
            : "Every well you can access on a real map: the Upper Assam wells and the public Norwegian register, synced daily, newest first."
        }
        badge={<Layers3 aria-hidden className="size-5 text-petrol" />}
      />
      <SignInGate>
        <ErrorNote error={wellsError} />
        <div role="tablist" aria-label="Map view" className="mb-4 inline-flex rounded-md border border-line bg-surface p-0.5 text-sm">
          {(
            [
              ["all", "All wells (live)"],
              ["offsets", w ? `Offsets of ${w.wellbore}` : "Offsets (select a well)"],
            ] as const
          ).map(([k, label]) => (
            <button
              key={k}
              type="button"
              role="tab"
              aria-selected={view === k}
              disabled={k === "offsets" && selected == null}
              onClick={() => setView(k)}
              className={cn("min-h-target rounded px-4 disabled:opacity-50", view === k ? "bg-teal-solid font-medium text-white" : "hover:bg-surface-2")}
            >
              {label}
            </button>
          ))}
        </div>
        {showOffsets ? (
          <OffsetMap key={selected} id={selected} />
        ) : !wells ? (
          <Skeleton className="h-[600px] w-full" />
        ) : (
          <>
            {selected == null && (
              <p className="m-0 mb-3 text-sm text-muted">Pick an Upper Assam well (map or top bar) to rank its analogues; pick a public well for its facts and links.</p>
            )}
            <AllWells wells={ok} />
          </>
        )}
      </SignInGate>
    </>
  );
}
