import { useEffect, useState } from "react";

// Hash routes: the static server needs no rewrite rules, and the PWA works offline.
//   #/<name>[/<id>][/<view>]   e.g. #/well/7/correlation
export const ROUTES = ["map", "well", "live", "plan", "ask", "voice", "review", "fleet", "replay", "admin"] as const;
export type RouteName = (typeof ROUTES)[number];
export type Route = { name: RouteName; id: number | null; view: string | null };

export function parse(hash: string): Route {
  const [, name, id, view] = /^#\/?([a-z]*)(?:\/(\d+))?(?:\/([a-z-]+))?/.exec(hash) ?? [];
  const r = (ROUTES as readonly string[]).includes(name) ? (name as RouteName) : "map";
  return { name: r, id: id ? Number(id) : null, view: view ?? null };
}

export function href(name: RouteName, id?: number | null, view?: string | null): string {
  return `#/${name}${id ? `/${id}` : ""}${view ? `/${view}` : ""}`;
}

export function useRoute(): Route {
  const [route, setRoute] = useState(() => parse(location.hash));
  useEffect(() => {
    const on = () => setRoute(parse(location.hash));
    window.addEventListener("hashchange", on);
    on(); // the hash may have changed before this listener existed (the sign-in callback)
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return route;
}
