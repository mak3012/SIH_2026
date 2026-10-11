import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, API } from "./api";
import { completeLogin, startRefresh, token, who, type Who } from "./auth";

export type Wellbore = {
  wellbore_id: number;
  well: string;
  wellbore: string;
  block: string;
  status: string | null;
  td_md: number | null;
  has_selection: boolean;
  feed_state: string | null;
  open_alerts: number;
  lat: number | null;
  lon: number | null;
  field: string | null;
};

export type Sample = { ts: string; rig_state: string; formation?: string | null; values: Record<string, number>; derived?: Record<string, number>; posterior?: Record<string, number> };
export type LiveAlert = { id: number; hazard: string; tier: number; tier_name: string; posterior: number; prior?: number; lr?: number; status?: string; citations?: { id: string; document?: string; page?: number; source_ref_id?: number }[] | null; sources?: { id: string; document?: string | null; page?: number | null; source_ref_id?: number | null; well?: string | null }[]; raised_at: string | null; cleared_at: string | null; formation?: string | null; rig_state?: string | null; explanation_status?: string; explanation?: string | null; features?: Record<string, number | null> };
export type FeedState = "live" | "stale" | "disconnected" | "stopped" | "none" | "connecting";

type Live = { feed: FeedState; sample: Sample | null; alerts: LiveAlert[]; lastDataAt: number | null; staleAfterS: number };

type App = {
  user: Who | null;
  wells: Wellbore[] | null;
  wellsError: unknown;
  reloadWells: () => void;
  selected: number | null;
  select: (id: number | null) => void;
  live: Live;
};

const Ctx = createContext<App | null>(null);
const SELECTED = "wi-selected";

/** One WebSocket for the selected well, shared by the top bar and the Live page. */
function useLive(id: number | null, signedIn: boolean): Live {
  const [live, setLive] = useState<Live>({ feed: "none", sample: null, alerts: [], lastDataAt: null, staleAfterS: 6 });
  const lastData = useRef(0);
  useEffect(() => {
    if (id == null || !signedIn) {
      setLive({ feed: "none", sample: null, alerts: [], lastDataAt: null, staleAfterS: 6 });
      return;
    }
    setLive((l) => ({ ...l, feed: "connecting", sample: null, alerts: [] }));
    const ws = new WebSocket(`${API.replace(/^http/, "ws")}/active/${id}/stream`);
    let staleAfter = 6;
    ws.onopen = () => ws.send(JSON.stringify({ type: "auth", token: token() ?? "" }));
    ws.onclose = () => setLive((l) => ({ ...l, feed: l.feed === "none" ? "none" : "disconnected" }));
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.type === "snapshot") {
        staleAfter = m.stale_after_s ?? 6;
        if (m.feed.state === "live") lastData.current = Date.now();
        const feed: FeedState = m.feed.connector ? m.feed.state : "none";
        setLive({ feed, sample: m.sample, alerts: m.alerts, lastDataAt: m.feed.state === "live" ? Date.now() : null, staleAfterS: staleAfter });
      } else if (m.type === "sample") {
        lastData.current = Date.now();
        setLive((l) => ({ ...l, feed: "live", sample: m, lastDataAt: Date.now() }));
      } else if (m.type === "feed") {
        setLive((l) => ({ ...l, feed: m.state }));
      } else if (m.type === "alert") {
        setLive((l) => ({ ...l, alerts: [m.alert, ...l.alerts.filter((a) => a.id !== m.alert.id)] }));
      }
    };
    // Client watchdog: grey even when the server cannot say so.
    const timer = setInterval(() => {
      setLive((l) => (l.feed === "live" && Date.now() - lastData.current > staleAfter * 1000 ? { ...l, feed: "stale" } : l));
    }, 1000);
    return () => {
      clearInterval(timer);
      ws.onclose = null;
      ws.close();
    };
  }, [id, signedIn]);
  return live;
}

export function AppProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<Who | null>(who());
  const [wells, setWells] = useState<Wellbore[] | null>(null);
  const [wellsError, setWellsError] = useState<unknown>(null);
  const [selected, setSelected] = useState<number | null>(() => {
    const v = Number(localStorage.getItem(SELECTED));
    return Number.isFinite(v) && v > 0 ? v : null;
  });

  useEffect(() => {
    completeLogin().then((done) => done && setUser(who()), setWellsError);
    return startRefresh();
  }, []);

  const reloadWells = useCallback(() => {
    if (!user) return;
    api<Wellbore[]>("/wellbores").then(setWells, setWellsError);
  }, [user]);
  useEffect(reloadWells, [reloadWells]);

  const select = useCallback((id: number | null) => {
    setSelected(id);
    try {
      if (id == null) localStorage.removeItem(SELECTED);
      else localStorage.setItem(SELECTED, String(id));
    } catch {
      /* not persisted */
    }
  }, []);

  const live = useLive(selected, !!user);
  const value = useMemo(() => ({ user, wells, wellsError, reloadWells, selected, select, live }), [user, wells, wellsError, reloadWells, selected, select, live]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useApp(): App {
  const c = useContext(Ctx);
  if (!c) throw new Error("useApp outside AppProvider");
  return c;
}

export function useSelectedWell(): Wellbore | null {
  const { wells, selected } = useApp();
  return wells?.find((w) => w.wellbore_id === selected) ?? null;
}
