import { useEffect, useRef, useState } from "react";

/** Width of an element in CSS pixels, so SVG is drawn at its real size (crisp text and lines,
 *  no viewBox scaling). */
export function useWidth<T extends HTMLElement>(initial = 800): [React.RefObject<T | null>, number] {
  const ref = useRef<T>(null);
  const [w, setW] = useState(initial);
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(280, Math.floor(e.contentRect.width))));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

/** Seconds since mount, ticking once a second (for countdowns). */
export function useNow(): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  return now;
}

export function hms(s: number | null | undefined): string {
  if (s == null || !Number.isFinite(s)) return "–";
  const v = Math.max(0, Math.round(s));
  const h = Math.floor(v / 3600);
  const m = Math.floor((v % 3600) / 60);
  return `${h} h ${String(m).padStart(2, "0")} min ${String(v % 60).padStart(2, "0")} s`;
}
