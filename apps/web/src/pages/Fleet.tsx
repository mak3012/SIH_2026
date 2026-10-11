import { Card, CardBody, CardHeader, cn, DataTable, FreshnessIndicator, Quantity, RiskTierBadge, Stat, tierOf } from "@repo/ui";
import { Activity } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { DataState, PageHeader, pct, SignInGate, useLoad, words } from "../components/common";
import { hms, useNow } from "../components/useWidth";
import { href } from "../router";
import { useApp } from "../state";

type Next = { formation: string; hazard: string; mean: number; state: string; distance_m?: number | null; eta_hours?: number | null };
type Rig = {
  wellbore_id: number;
  well: string;
  block: string;
  feed_state: string;
  connector: string | null;
  last_sample_ts: string | null;
  age_s: number | null;
  current_md: number | null;
  rate_m_per_hr: number | null;
  rate_basis: string;
  next_hazard: Next | null;
  alerts: { advisory: number; warning: number; critical: number };
};
type DigestItem = { id: number; well: string; hazard: string; tier: number; status: string; raised_at: string | null; cleared_at: string | null; ack_by: string | null; suppressed: string | null; peak_posterior: number | null };
type Fleet = { as_of: string; rigs: Rig[]; digest: { hours: number; raised: number; acknowledged: number; cleared: number; shelved: number; by_tier: Record<string, number>; items: DigestItem[] } };

const age = (s: number | null) => (s == null ? undefined : s < 120 ? `${Math.max(0, s)} s` : s < 7200 ? `${Math.round(s / 60)} min` : `${Math.round(s / 3600)} h`);

function AlertLoad({ a }: { a: Rig["alerts"] }) {
  const total = a.advisory + a.warning + a.critical;
  if (!total) return <span className="text-sm text-muted">no open alerts</span>;
  return (
    <span className="flex flex-wrap gap-1.5">
      {a.critical > 0 && <RiskTierBadge tier="critical">{` ${a.critical}`}</RiskTierBadge>}
      {a.warning > 0 && <RiskTierBadge tier="warning">{` ${a.warning}`}</RiskTierBadge>}
      {a.advisory > 0 && <RiskTierBadge tier="advisory">{` ${a.advisory}`}</RiskTierBadge>}
    </span>
  );
}

function RigCard({ r, loadedAt, now }: { r: Rig; loadedAt: number; now: number }) {
  const { select } = useApp();
  const live = r.feed_state === "live";
  const stale = ["stale", "disconnected"].includes(r.feed_state);
  const n = r.next_hazard;
  const eta = n?.eta_hours != null ? n.eta_hours * 3600 - (now - loadedAt) / 1000 : null;
  return (
    <Card className={cn("h-full", r.alerts.critical ? "border-2 border-critical-solid" : r.alerts.warning ? "border-2 border-warning-solid" : "", stale && "opacity-70 grayscale")} data-testid={`rig-${r.well}`}>
      <CardBody className="grid gap-3">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <a href={href(live ? "live" : "well", r.wellbore_id)} onClick={() => select(r.wellbore_id)} className="num whitespace-nowrap text-xl font-semibold">
            {r.well}
          </a>
          <span className="whitespace-nowrap text-sm text-muted">{r.block}</span>
          <span className="ml-auto whitespace-nowrap">
            <FreshnessIndicator state={(r.feed_state as "live") ?? "none"} age={live ? undefined : age(r.age_s)} />
          </span>
        </div>
        <div className="flex flex-wrap gap-x-5 gap-y-1 text-sm">
          <span>
            <span className="text-muted">Bit </span>
            <Quantity value={r.current_md} unit="m MD" digits={0} />
          </span>
          <span>
            <span className="text-muted">ROP </span>
            <Quantity value={r.rate_m_per_hr} unit="m/h" digits={1} />
          </span>
        </div>
        <div className={cn("rounded-md border px-3 py-2", n ? "border-gold-solid bg-gold-subtle" : "border-line")}>
          <div className="text-xs font-semibold uppercase tracking-wide text-muted">Next hazard</div>
          {n ? (
            <>
              <div className="font-semibold">
                {n.formation} · {words(n.hazard)} <span className="num">{pct(n.mean)}</span>
              </div>
              <div className="num text-sm">
                {n.state === "in band" ? <span className="font-semibold text-warning">bit in band</span> : <>{n.distance_m != null ? `${Math.round(n.distance_m).toLocaleString()} m · ` : ""}ETA {eta != null ? hms(eta) : "no rate"}</>}
              </div>
            </>
          ) : (
            <div className="text-sm text-muted">none ahead above 10 %</div>
          )}
        </div>
        <div className="flex items-center justify-between gap-2">
          <span className="text-xs font-semibold uppercase tracking-wide text-muted">Alert load</span>
          <AlertLoad a={r.alerts} />
        </div>
      </CardBody>
    </Card>
  );
}

function Digest({ d }: { d: Fleet["digest"] }) {
  return (
    <Card>
      <CardHeader title={`Shift digest · last ${d.hours} h`} description="Alerts raised, acknowledged, back to normal and shelved across the fleet." />
      <CardBody className="grid gap-4">
        <div className="flex flex-wrap gap-6" data-testid="digest">
          <Stat label="Raised">{d.raised}</Stat>
          <Stat label="Acknowledged">{d.acknowledged}</Stat>
          <Stat label="Back to normal">{d.cleared}</Stat>
          <Stat label="Shelved">{d.shelved}</Stat>
          <div className="flex items-end gap-2">
            {(["critical", "warning", "advisory"] as const).map((t) => (
              <RiskTierBadge key={t} tier={t}>{` ${d.by_tier[t] ?? 0}`}</RiskTierBadge>
            ))}
          </div>
        </div>
        {d.items.length > 0 && (
          <DataTable
            caption="Alerts this shift"
            head={["Well", "Hazard", "Tier", "Peak P", "Raised", "Status"]}
            rows={d.items.slice(0, 12).map((x) => [
              <span key="w" className="num">{x.well}</span>,
              words(x.hazard),
              <RiskTierBadge key="t" tier={tierOf(x.tier)} />,
              <span key="p" className="num">{pct(x.peak_posterior)}</span>,
              <span key="r" className="num">{x.raised_at ? new Date(x.raised_at).toLocaleString() : "–"}</span>,
              x.suppressed ? `shelved` : x.ack_by ? `acknowledged by ${x.ack_by}` : x.cleared_at ? "back to normal" : x.status,
            ])}
          />
        )}
      </CardBody>
    </Card>
  );
}

function FleetBody() {
  const state = useLoad<Fleet>("/fleet");
  const [loadedAt, setLoadedAt] = useState(Date.now());
  const now = useNow();
  useEffect(() => {
    if (state.data) setLoadedAt(Date.now());
  }, [state.data]);
  useEffect(() => {
    const t = setInterval(state.reload, 30_000);
    return () => clearInterval(t);
  }, [state.reload]);
  const rigs = useMemo(() => {
    const score = (r: Rig) => r.alerts.critical * 1e6 + r.alerts.warning * 1e4 + r.alerts.advisory * 100 + (r.feed_state === "live" ? 50 : 0);
    const eta = (r: Rig) => r.next_hazard?.eta_hours ?? 1e9;
    return [...(state.data?.rigs ?? [])].sort((a, b) => score(b) - score(a) || eta(a) - eta(b) || a.well.localeCompare(b.well));
  }, [state.data]);
  return (
    <DataState state={state} isEmpty={(d) => d.rigs.length === 0} empty={{ title: "No rigs to show", body: "No well you can access has a real-time feed or an offset selection.", icon: <Activity /> }}>
      {(d) => (
        <div className="grid gap-4">
          <div className="flex flex-wrap gap-6">
            <Stat label="Rigs">{d.rigs.length}</Stat>
            <Stat label="Live feeds" accent="cyan">{d.rigs.filter((r) => r.feed_state === "live").length}</Stat>
            <Stat label="Rigs with open alerts">{d.rigs.filter((r) => r.alerts.critical + r.alerts.warning + r.alerts.advisory > 0).length}</Stat>
            <Stat label="Next hazard within 24 h" accent="gold">{d.rigs.filter((r) => (r.next_hazard?.eta_hours ?? 1e9) <= 24).length}</Stat>
          </div>
          <ul className="m-0 grid list-none gap-3 p-0 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4" aria-label="Rigs, most urgent first">
            {rigs.map((r) => (
              <li key={r.wellbore_id}>
                <RigCard r={r} loadedAt={loadedAt} now={now} />
              </li>
            ))}
          </ul>
          <Digest d={d.digest} />
        </div>
      )}
    </DataState>
  );
}

export function FleetPage() {
  return (
    <>
      <PageHeader title="Fleet board" description="Every rig you can access, most urgent first: open alerts, the next hazard ahead of the bit with its ETA, and data freshness. Refreshes every 30 s." />
      <SignInGate>
        <FleetBody />
      </SignInGate>
    </>
  );
}
