import type { Config } from "tailwindcss";
import { brand, earth, font, radius, risk, slate } from "./tokens";

const MEANINGS = ["petrol", "sepia", "cyan", "violet", "teal", "gold", "advisory", "warning", "critical"] as const;

/**
 * Tailwind preset from tokens.ts. Theme-dependent colours are CSS variables (set by
 * src/theme.css for data-theme="office" | "rig"), so one class works in both themes:
 *   bg-surface, text-fg, text-muted, border-line          neutrals
 *   text-petrol, bg-petrol-subtle, border-petrol, ...      meaning: AA text / tint / brand
 *   bg-petrol-solid                                        the brand value itself
 */
export const preset: Partial<Config> = {
  darkMode: ["selector", '[data-theme="rig"]'],
  theme: {
    extend: {
      colors: {
        bg: "var(--bg)",
        surface: "var(--surface)",
        "surface-2": "var(--surface-2)",
        line: "var(--border)",
        "line-strong": "var(--border-strong)",
        fg: "var(--text)",
        muted: "var(--muted)",
        focus: "var(--focus)",
        slate,
        earth: { 1: earth[0], 2: earth[1], 3: earth[2] },
        ...Object.fromEntries(
          MEANINGS.map((m) => [
            m,
            {
              DEFAULT: `var(--${m})`,
              subtle: `var(--${m}-subtle)`,
              solid: (brand as Record<string, string>)[m] ?? (risk as Record<string, string>)[m],
            },
          ]),
        ),
      },
      fontFamily: { sans: [...font.sans], mono: [...font.mono] },
      borderRadius: { sm: `${radius.sm}px`, DEFAULT: `${radius.md}px`, md: `${radius.md}px`, lg: `${radius.lg}px` },
      minHeight: { target: "var(--target)" },
      minWidth: { target: "var(--target)" },
      height: { target: "var(--target)" },
      transitionDuration: { fast: "150ms", base: "200ms", slow: "250ms" },
      boxShadow: { card: "0 1px 2px rgb(15 23 42 / 0.06), 0 1px 1px rgb(15 23 42 / 0.04)" },
    },
  },
};

export default preset;
