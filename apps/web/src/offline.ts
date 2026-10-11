import { get, set } from "idb-keyval";
import { api, ApiError } from "./api";

// Hazard package: what a rig screen needs when the link to shore drops. Each GET below is
// stored in IndexedDB on success; when the network fails it is served from there, marked with
// the time it was cached. Nothing is written while offline.

export const PACKAGE = (id: number) => [
  `/active/${id}/hazard-profile`,
  `/active/${id}/lookahead`,
  `/active/${id}/alerts`,
  `/wellbores/${id}/scene`,
];

export type Cached<T> = { data: T; cachedAt: string | null };

const isNetwork = (e: unknown) => e instanceof TypeError || (typeof navigator !== "undefined" && !navigator.onLine);

/** GET with an offline fallback. API errors (403, 404, 409) are never masked by the cache. */
export async function cachedGet<T>(path: string): Promise<Cached<T>> {
  try {
    const data = await api<T>(path);
    await set(`pkg:${path}`, { data, at: new Date().toISOString() }).catch(() => undefined);
    return { data, cachedAt: null };
  } catch (e) {
    if (e instanceof ApiError || !isNetwork(e)) throw e;
    const hit = (await get(`pkg:${path}`).catch(() => undefined)) as { data: T; at: string } | undefined;
    if (!hit) throw e;
    return { data: hit.data, cachedAt: hit.at };
  }
}

/** Fetch every part of a well's hazard package now (Rig: "Save for offline"). */
export async function savePackage(id: number): Promise<string[]> {
  const failed: string[] = [];
  for (const p of PACKAGE(id)) {
    try {
      await cachedGet(p);
    } catch {
      failed.push(p);
    }
  }
  await set(`pkg:saved:${id}`, new Date().toISOString());
  return failed;
}

export async function savedAt(id: number): Promise<string | null> {
  return ((await get(`pkg:saved:${id}`).catch(() => null)) as string | null) ?? null;
}
