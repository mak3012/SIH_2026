/**
 * Design tokens: the single source of colours, type, spacing, motion and target sizes.
 * Tailwind (tailwind-preset.ts) and the theme CSS (src/theme.css) are built from these.
 *
 * Colour carries meaning, never decoration:
 *   petrol  memory / data          sepia   documents / history
 *   cyan    live / eRTMAC           violet  AI
 *   teal    people / UI actions     gold    showstoppers / evidence
 *   risk tiers advisory / warning / critical (always with an icon and a label)
 * Each meaning has a `base` (the brand value: fills, icons, borders, large text) and a `text`
 * shade per theme that meets WCAG 2.2 AA (4.5:1) for body text on that theme's surfaces.
 */

export const brand = {
  petrol: "#0B3C5D",
  sepia: "#9A6B3F",
  cyan: "#0891B2",
  violet: "#6D28D9",
  teal: "#0F766E",
  gold: "#EAB308",
} as const;

export const risk = {
  advisory: "#2563EB",
  warning: "#EA580C",
  critical: "#DC2626",
} as const;

export const slate = {
  50: "#F8FAFC",
  100: "#F1F5F9",
  200: "#E2E8F0",
  300: "#CBD5E1",
  400: "#94A3B8",
  500: "#64748B",
  600: "#475569",
  700: "#334155",
  800: "#1E293B",
  900: "#0F172A",
} as const;

/** Formation earth tones, shallow to deep (cycled for more formations). */
export const earth = ["#EFE6D2", "#E2D3B5", "#D3C6AE"] as const;

export type Meaning = keyof typeof brand | keyof typeof risk;

/** Per-theme semantic colours. `text` shades are AA on that theme's bg and surface. */
export const themes = {
  office: {
    label: "Office",
    bg: slate[50],
    surface: "#FFFFFF",
    surface2: slate[100],
    border: slate[200],
    borderStrong: slate[300],
    text: slate[900],
    muted: slate[600],
    focus: brand.cyan,
    text_: {
      petrol: brand.petrol,
      sepia: "#7A5230",
      cyan: "#0E7490",
      violet: brand.violet,
      teal: brand.teal,
      gold: "#854D0E",
      advisory: "#1D4ED8",
      warning: "#C2410C",
      critical: "#B91C1C",
    },
    subtle: {
      petrol: "#E6EEF4",
      sepia: "#F5EEE6",
      cyan: "#E0F4F8",
      violet: "#EFE9FB",
      teal: "#E2F2EF",
      gold: "#FDF6D8",
      advisory: "#E5EDFD",
      warning: "#FDEDE4",
      critical: "#FCE8E8",
    },
  },
  rig: {
    label: "Rig",
    bg: "#05080F",
    surface: "#0B1220",
    surface2: "#121B2E",
    border: "#26324A",
    borderStrong: "#3B4A68",
    text: "#F8FAFC",
    muted: "#C2CCDA",
    focus: "#22D3EE",
    text_: {
      petrol: "#8FC3E6",
      sepia: "#E2B78C",
      cyan: "#22D3EE",
      violet: "#B9A2FB",
      teal: "#2DD4BF",
      gold: "#FACC15",
      advisory: "#7AB2FF",
      warning: "#FB923C",
      critical: "#FF7A7A",
    },
    subtle: {
      petrol: "#0E2A40",
      sepia: "#2E2116",
      cyan: "#062F38",
      violet: "#22173F",
      teal: "#06302B",
      gold: "#332A06",
      advisory: "#0F2350",
      warning: "#3A1A08",
      critical: "#3F0F0F",
    },
  },
} as const;

export type ThemeName = keyof typeof themes;

export const font = {
  sans: ['"IBM Plex Sans"', "Inter", "system-ui", "-apple-system", '"Segoe UI"', "sans-serif"],
  mono: ['"IBM Plex Mono"', '"JetBrains Mono"', "ui-monospace", "Consolas", "monospace"],
} as const;

/** Type scale (px). Rig mode scales the root size up (theme.css), so all of it grows. */
export const fontSize = { xs: 12, sm: 13, base: 15, md: 17, lg: 20, xl: 24, "2xl": 30, "3xl": 38 } as const;

export const space = { 0: 0, 1: 4, 2: 8, 3: 12, 4: 16, 5: 20, 6: 24, 8: 32, 10: 40, 12: 48 } as const;

export const radius = { sm: 4, md: 6, lg: 10, full: 9999 } as const;

/** Minimum touch target (px): 40 in Office (WCAG 2.5.8 asks 24), 48 in Rig (gloves). */
export const target = { office: 40, rig: 48 } as const;

/** Motion: subtle, short, and none at all on the rig screen or with reduced motion. */
export const motion = {
  fast: 0.15,
  base: 0.2,
  slow: 0.25,
  ease: [0.2, 0, 0, 1] as [number, number, number, number],
} as const;
