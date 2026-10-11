import { useCallback, useSyncExternalStore } from "react";
import { brand, risk, target, themes, type ThemeName } from "../tokens";

const KEY = "wi-theme";
const MEANINGS = ["petrol", "sepia", "cyan", "violet", "teal", "gold", "advisory", "warning", "critical"] as const;

/** Set the theme's CSS variables on <html> (from tokens.ts, so CSS and tokens cannot drift). */
export function applyTheme(name: ThemeName): void {
  const t = themes[name];
  const s = document.documentElement.style;
  const set = (k: string, v: string) => s.setProperty(`--${k}`, v);
  set("bg", t.bg);
  set("surface", t.surface);
  set("surface-2", t.surface2);
  set("border", t.border);
  set("border-strong", t.borderStrong);
  set("text", t.text);
  set("muted", t.muted);
  set("focus", t.focus);
  set("target", `${target[name]}px`);
  for (const m of MEANINGS) {
    set(m, t.text_[m]);
    set(`${m}-subtle`, t.subtle[m]);
    set(`${m}-solid`, (brand as Record<string, string>)[m] ?? (risk as Record<string, string>)[m]);
  }
  document.documentElement.dataset.theme = name;
  document.documentElement.style.colorScheme = name === "rig" ? "dark" : "light";
}

export function storedTheme(): ThemeName {
  try {
    const v = localStorage.getItem(KEY);
    if (v === "office" || v === "rig") return v;
  } catch {
    /* storage blocked: fall through */
  }
  return "office";
}

const listeners = new Set<() => void>();

export function setTheme(name: ThemeName): void {
  try {
    localStorage.setItem(KEY, name);
  } catch {
    /* not persisted */
  }
  applyTheme(name);
  listeners.forEach((l) => l());
}

/** Current theme and a setter; Rig mode also disables motion (see motionOk). */
export function useTheme(): [ThemeName, (t: ThemeName) => void] {
  const theme = useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => (document.documentElement.dataset.theme as ThemeName) || "office",
    () => "office" as ThemeName,
  );
  return [theme, useCallback((t: ThemeName) => setTheme(t), [])];
}

/** Decorative/transition motion allowed? Never on the rig screen or with reduced motion. */
export function motionOk(): boolean {
  if (typeof window === "undefined") return false;
  if (document.documentElement.dataset.theme === "rig") return false;
  return !window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
}
