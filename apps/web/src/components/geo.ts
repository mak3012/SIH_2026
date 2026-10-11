// Small geodesy helpers for the map (WGS 84, distances in metres; accurate to well within a
// pixel at field scale).
const R = 6_371_008.8;

export type LonLat = [number, number];

/** A circle of radius `m` metres around a point, as a closed ring. */
export function circle([lon, lat]: LonLat, m: number, n = 96): LonLat[] {
  const out: LonLat[] = [];
  const d = m / R;
  const φ1 = (lat * Math.PI) / 180;
  const λ1 = (lon * Math.PI) / 180;
  for (let i = 0; i <= n; i++) {
    const θ = (2 * Math.PI * i) / n;
    const φ2 = Math.asin(Math.sin(φ1) * Math.cos(d) + Math.cos(φ1) * Math.sin(d) * Math.cos(θ));
    const λ2 = λ1 + Math.atan2(Math.sin(θ) * Math.sin(d) * Math.cos(φ1), Math.cos(d) - Math.sin(φ1) * Math.sin(φ2));
    out.push([(λ2 * 180) / Math.PI, (φ2 * 180) / Math.PI]);
  }
  return out;
}

/** Convex hull (monotone chain). */
export function hull(pts: LonLat[]): LonLat[] {
  const p = [...pts].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  if (p.length < 3) return p;
  const cross = (o: LonLat, a: LonLat, b: LonLat) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const half = (pts: LonLat[]) => {
    const h: LonLat[] = [];
    for (const q of pts) {
      while (h.length >= 2 && cross(h[h.length - 2], h[h.length - 1], q) <= 0) h.pop();
      h.push(q);
    }
    return h.slice(0, -1);
  };
  return [...half(p), ...half([...p].reverse())];
}

/** An outline around a group of wells: the hull of a small circle around each one. Drawn
 *  around the wells, so it is where the wells are, not an interpreted boundary. */
export function outline(pts: LonLat[], padM = 1200): LonLat[] {
  const ring = hull(pts.flatMap((p) => circle(p, padM, 16)));
  return ring.length ? [...ring, ring[0]] : ring;
}

/** Great-circle distance in metres. */
export function distance([lon1, lat1]: LonLat, [lon2, lat2]: LonLat): number {
  const r = Math.PI / 180;
  const a = Math.sin(((lat2 - lat1) * r) / 2) ** 2 + Math.cos(lat1 * r) * Math.cos(lat2 * r) * Math.sin(((lon2 - lon1) * r) / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}

/** Greedy label thinning: keep an item only if no kept item lies within `m` metres. Items
 * earlier in the list win, so put the important ones first. */
export function declutter<T>(items: T[], at: (t: T) => LonLat, m: number): T[] {
  const kept: T[] = [];
  for (const t of items) if (kept.every((k) => distance(at(k), at(t)) >= m)) kept.push(t);
  return kept;
}

export function bounds(pts: LonLat[]): [LonLat, LonLat] {
  const xs = pts.map((p) => p[0]);
  const ys = pts.map((p) => p[1]);
  return [
    [Math.min(...xs), Math.min(...ys)],
    [Math.max(...xs), Math.max(...ys)],
  ];
}
