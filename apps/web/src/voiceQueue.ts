import { del, entries, set } from "idb-keyval";
import { api } from "./api";

// Recordings wait on the device (IndexedDB) until they are uploaded, so a note taken on the
// rig floor without signal is not lost. Uploads are idempotent on client_id.

export type Queued = {
  client_id: string;
  wellbore_id: number;
  well: string;
  formation: string | null;
  note: string;
  recorded_at: string;
  duration_s: number;
  blob: Blob;
  attempts: number;
  last_error?: string;
};

const KEY = (id: string) => `voice:${id}`;

export async function enqueue(q: Omit<Queued, "attempts">): Promise<void> {
  await set(KEY(q.client_id), { ...q, attempts: 0 });
}

export async function queued(): Promise<Queued[]> {
  return (await entries<string, Queued>())
    .filter(([k]) => String(k).startsWith("voice:"))
    .map(([, v]) => v)
    .sort((a, b) => a.recorded_at.localeCompare(b.recorded_at));
}

export async function remove(id: string): Promise<void> {
  await del(KEY(id));
}

/** Upload everything queued; returns how many went. Stops quietly when offline. */
export async function flush(): Promise<number> {
  if (!navigator.onLine) return 0;
  let sent = 0;
  for (const q of await queued()) {
    const params = new URLSearchParams({
      wellbore_id: String(q.wellbore_id),
      client_id: q.client_id,
      duration_s: q.duration_s.toFixed(1),
      recorded_at: q.recorded_at,
      ...(q.formation ? { formation: q.formation } : {}),
      ...(q.note ? { note: q.note } : {}),
    });
    try {
      await api(`/voice-notes?${params}`, { method: "POST", body: q.blob, contentType: q.blob.type || "audio/webm" });
      await remove(q.client_id);
      sent++;
    } catch (e) {
      await set(KEY(q.client_id), { ...q, attempts: q.attempts + 1, last_error: e instanceof Error ? e.message : String(e) });
      if (!navigator.onLine) break;
    }
  }
  return sent;
}
