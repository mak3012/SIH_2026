import { cn, FreshnessIndicator, NativeSelect, RigStatePill, ThemeToggle } from "@repo/ui";
import {
  Activity,
  ClipboardList,
  History,
  Layers,
  LayoutGrid,
  ListChecks,
  LogIn,
  LogOut,
  Map as MapIcon,
  Menu,
  MessagesSquare,
  Mic,
  Settings,
  X,
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { APP_ROLES, login, logout } from "./auth";
import { href, type Route, type RouteName } from "./router";
import { useApp } from "./state";

const NAV: { name: RouteName; label: string; icon: typeof MapIcon; well?: boolean }[] = [
  { name: "map", label: "Map", icon: MapIcon },
  { name: "well", label: "Well", icon: Layers, well: true },
  { name: "live", label: "Live", icon: Activity, well: true },
  { name: "plan", label: "Plan", icon: ClipboardList, well: true },
  { name: "ask", label: "Ask", icon: MessagesSquare },
  { name: "voice", label: "Voice note", icon: Mic },
  { name: "review", label: "Review", icon: ListChecks },
  { name: "fleet", label: "Fleet", icon: LayoutGrid },
  { name: "replay", label: "Replay", icon: History },
  { name: "admin", label: "Admin", icon: Settings },
];

function useAge(at: number | null): string | undefined {
  const [, tick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, []);
  if (at == null) return undefined;
  const s = Math.max(0, Math.round((Date.now() - at) / 1000));
  return s < 120 ? `${s} s` : `${Math.round(s / 60)} min`;
}

function TopBar({ onMenu }: { onMenu: () => void }) {
  const { user, wells, selected, select, live } = useApp();
  const age = useAge(live.lastDataAt);
  const feed = live.feed === "connecting" ? "disconnected" : live.feed;
  return (
    <header className="sticky top-0 z-30 flex flex-wrap items-center gap-3 border-b border-line bg-surface px-4 py-2">
      <button type="button" className="inline-flex min-h-target min-w-target items-center justify-center rounded-md hover:bg-surface-2 lg:hidden" onClick={onMenu} aria-label="Open navigation">
        <Menu aria-hidden className="size-5" />
      </button>
      {user && (
        <label className="flex min-w-[130px] flex-1 items-center sm:min-w-[220px] sm:flex-none gap-2 text-sm">
          <span className="sr-only">Well</span>
          <NativeSelect
            aria-label="Selected well"
            value={selected ?? ""}
            onChange={(e) => select(e.target.value ? Number(e.target.value) : null)}
            className="max-w-[280px] font-mono"
          >
            <option value="">Select a well…</option>
            {(wells ?? []).map((w) => (
              <option key={w.wellbore_id} value={w.wellbore_id}>
                {w.wellbore} · {w.block}
              </option>
            ))}
          </NativeSelect>
        </label>
      )}
      {selected != null && (
        <>
          <span className="hidden sm:inline-flex">
            <RigStatePill state={live.sample?.rig_state} />
          </span>
          <FreshnessIndicator state={feed} age={feed === "live" ? undefined : age} />
        </>
      )}
      <div className="ml-auto flex flex-wrap items-center gap-2">
        {user ? (
          <span className="hidden text-sm sm:inline">
            <span className="font-semibold">{user.name}</span>{" "}
            <span className="text-muted">· {user.roles.filter((r) => APP_ROLES.includes(r)).map((r) => r.replace("_", " ")).join(", ") || "no role"}</span>
          </span>
        ) : null}
        <ThemeToggle />
        {user ? (
          <button type="button" onClick={logout} className="inline-flex min-h-target items-center gap-2 rounded-md px-3 text-sm hover:bg-surface-2">
            <LogOut aria-hidden className="size-4" /> <span className="sr-only sm:not-sr-only">Sign out</span>
          </button>
        ) : (
          <>
            <button type="button" onClick={() => void login(true)} className="inline-flex min-h-target items-center rounded-md px-3 text-sm hover:bg-surface-2">
              Sign up
            </button>
            <button type="button" onClick={() => void login()} className="inline-flex min-h-target items-center gap-2 rounded-md bg-teal-solid px-3 text-sm font-medium text-white">
              <LogIn aria-hidden className="size-4" /> Sign in
            </button>
          </>
        )}
      </div>
    </header>
  );
}

function Nav({ route, onNavigate }: { route: Route; onNavigate: () => void }) {
  const { selected } = useApp();
  return (
    <nav aria-label="Main" className="flex flex-col gap-1 p-3">
      <a href="#/map" className="mb-3 flex items-center gap-2 px-2 text-md font-semibold text-fg no-underline" onClick={onNavigate}>
        <img src="/icon.svg" alt="" width={28} height={28} className="rounded-md" />
        Well Intelligence
      </a>
      {NAV.map((n) => {
        const active = route.name === n.name;
        const Icon = n.icon;
        return (
          <a
            key={n.name}
            href={href(n.name, n.well ? selected : null)}
            aria-current={active ? "page" : undefined}
            onClick={onNavigate}
            className={cn(
              "flex min-h-target items-center gap-3 rounded-md px-3 text-base no-underline",
              active ? "bg-teal-subtle font-semibold text-teal" : "text-fg hover:bg-surface-2",
            )}
          >
            <Icon aria-hidden className="size-5 shrink-0" />
            {n.label}
          </a>
        );
      })}
    </nav>
  );
}

export function Shell({ route, children }: { route: Route; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[240px_1fr]">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded-md focus:bg-surface focus:px-3 focus:py-2">
        Skip to content
      </a>
      <aside className="hidden border-r border-line bg-surface lg:block">
        <div className="sticky top-0">
          <Nav route={route} onNavigate={() => undefined} />
        </div>
      </aside>
      {open && (
        <div className="fixed inset-0 z-40 lg:hidden" role="dialog" aria-modal="true" aria-label="Navigation">
          <div className="absolute inset-0 bg-slate-900/40" onClick={() => setOpen(false)} />
          <aside className="absolute inset-y-0 left-0 w-72 border-r border-line bg-surface">
            <button type="button" onClick={() => setOpen(false)} aria-label="Close navigation" className="absolute right-2 top-2 inline-flex min-h-target min-w-target items-center justify-center rounded-md hover:bg-surface-2">
              <X aria-hidden className="size-5" />
            </button>
            <Nav route={route} onNavigate={() => setOpen(false)} />
          </aside>
        </div>
      )}
      <div className="flex min-w-0 flex-col">
        <TopBar onMenu={() => setOpen(true)} />
        <main id="main" tabIndex={-1} className="mx-auto w-full max-w-[1400px] flex-1 px-4 py-5 focus:outline-none">
          {children}
        </main>
      </div>
    </div>
  );
}
