# Design system

`packages/ui` holds the design tokens, the Tailwind preset built from them, the theme runtime
and the components. The web app uses nothing else for colour, type, spacing or motion: change a
token in `packages/ui/tokens.ts` and both the app and Storybook follow.

```
packages/ui/
  tokens.ts            colours, type, spacing, radius, motion, touch targets (the source)
  tailwind-preset.ts   Tailwind theme from the tokens (colours are CSS variables)
  src/theme.ts         applyTheme / useTheme / motionOk: writes the CSS variables per theme
  src/theme.css        base styles, focus ring, .num, reduced motion, Rig rules
  src/primitives.tsx   Button, Card, Input, NativeSelect, Field, Tabs, Tip, Sheet, DataTable, Notice, …
  src/domain.tsx       risk-tier, S1–S6, G1–G4, R | AI, evidence chip, freshness, rig state, Quantity, …
  stories/             Storybook: Foundations, Components, Patterns (do / don't)
```

## Principles

1. **Colour means something.** Every hue has one meaning (below). Decoration is slate.
2. **Colour is never the only signal.** Risk tiers carry an icon and a word; badges carry a
   label; the freshness indicator says "Data stale" as well as turning grey; formation swatches
   carry the name.
3. **Numbers carry units**, in a monospaced tabular face (`Quantity`, `.num`): `2,153.1 m MD`,
   `1.25 sg`, `41 %`. Depths, IDs, units and citations are always mono.
4. **Show where it came from.** A value from a document, voice note or query has an evidence
   chip that opens the source. Where rules and an LLM worked together, an **R | AI** chip says
   which did what.
5. **The rig screen is calm.** No decorative motion; large targets; high contrast.

## Colour

### Meanings

| Token | Hex | Means | Used for |
|---|---|---|---|
| `petrol` | `#0B3C5D` | memory / data | offsets, roadmap intensity, G1–G4 shields, record counts |
| `sepia` | `#9A6B3F` | documents / history | document and voice-note evidence chips, extraction |
| `cyan` | `#0891B2` | live / eRTMAC | live feed, bit position, live posterior |
| `violet` | `#6D28D9` | AI | the AI half of **R \| AI**, AI actions (Write briefing) |
| `teal` | `#0F766E` | people / UI | primary buttons, selection, the active well, sign in |
| `gold` | `#EAB308` | showstoppers / evidence | S1–S6 hexagons, the cited box on a page, events in 3D, offline banner |

### Risk tiers

| Token | Hex | Icon | Word |
|---|---|---|---|
| `advisory` | `#2563EB` | info circle | Advisory |
| `warning` | `#EA580C` | triangle | Warning |
| `critical` | `#DC2626` | octagon | Critical |

Thresholds for live alerts are the alarm policy's (0.2 / 0.5 / 0.8 posterior); plan findings map
`block → critical`, `warn → warning`, `info → advisory`.

### Text shades (WCAG 2.2 AA)

Brand values are for fills, icons, borders and large text. Body text in a meaning colour uses the
theme's AA shade (≥ 4.5:1 on that theme's `bg` and `surface`), exposed as `text-<meaning>`;
`bg-<meaning>-subtle` is the tint behind it and `bg-<meaning>-solid` the brand value.

| Meaning | Office text | Office tint | Rig text | Rig tint |
|---|---|---|---|---|
| petrol | `#0B3C5D` | `#E6EEF4` | `#8FC3E6` | `#0E2A40` |
| sepia | `#7A5230` | `#F5EEE6` | `#E2B78C` | `#2E2116` |
| cyan | `#0E7490` | `#E0F4F8` | `#22D3EE` | `#062F38` |
| violet | `#6D28D9` | `#EFE9FB` | `#B9A2FB` | `#22173F` |
| teal | `#0F766E` | `#E2F2EF` | `#2DD4BF` | `#06302B` |
| gold | `#854D0E` | `#FDF6D8` | `#FACC15` | `#332A06` |
| advisory | `#1D4ED8` | `#E5EDFD` | `#7AB2FF` | `#0F2350` |
| warning | `#C2410C` | `#FDEDE4` | `#FB923C` | `#3A1A08` |
| critical | `#B91C1C` | `#FCE8E8` | `#FF7A7A` | `#3F0F0F` |

### Neutrals and themes

| Role | Office | Rig |
|---|---|---|
| `bg` | slate 50 `#F8FAFC` | `#05080F` |
| `surface` | `#FFFFFF` | `#0B1220` |
| `surface-2` | slate 100 `#F1F5F9` | `#121B2E` |
| `line` | slate 200 `#E2E8F0` | `#26324A` |
| `line-strong` | slate 300 `#CBD5E1` | `#3B4A68` |
| `fg` | slate 900 `#0F172A` | `#F8FAFC` |
| `muted` | slate 600 `#475569` | `#C2CCDA` |
| `focus` | cyan `#0891B2` | `#22D3EE` |

Slate 50–900 is the full neutral ramp (`#F8FAFC #F1F5F9 #E2E8F0 #CBD5E1 #94A3B8 #64748B
#475569 #334155 #1E293B #0F172A`). Formation earth tones, shallow to deep and cycled:
`#EFE6D2 #E2D3B5 #D3C6AE`.

**Office** is the light theme for desks. **Rig** is dark and high contrast for the driller's
cabin and sunlight on a tablet: near-black background, near-white text, 18 px root size,
48 px minimum targets (gloves), no transitions. The toggle in the top bar stores the choice
(`localStorage` `wi-theme`); the theme is applied before the first paint.

## Type

| Token | Value |
|---|---|
| Sans (UI) | IBM Plex Sans 400 / 600, then Inter, system-ui |
| Mono (depths, units, IDs, citations) | IBM Plex Mono 400 / 600, then JetBrains Mono, ui-monospace |
| Scale (px) | xs 12 · sm 13 · base 15 · md 17 · lg 20 · xl 24 · 2xl 30 · 3xl 38 (Rig scales the root up) |

Fonts are bundled (`@fontsource`), so the offline PWA renders the same.

## Space, radius, targets, motion

| Token | Values |
|---|---|
| Space (px) | 0 · 4 · 8 · 12 · 16 · 20 · 24 · 32 · 40 · 48 |
| Radius (px) | sm 4 · md 6 · lg 10 · full |
| Touch target (px) | Office 40 · Rig 48 (`min-h-target`, from `--target`) |
| Motion (s) | fast 0.15 · base 0.20 · slow 0.25, ease `cubic-bezier(0.2, 0, 0, 1)` |

Motion is framer-motion, used for page entry (fade + 4 px rise, 180 ms) and sheets. `motionOk()`
is false in Rig mode and under `prefers-reduced-motion`; then nothing moves.

## Components

The gallery is Storybook (`npm run storybook -w packages/ui`, or `build-storybook` to
`packages/ui/storybook-static`): **Foundations** (colours, AA text shades, type, spacing, motion),
**Components** (every component below in both themes, via the toolbar) and **Patterns** (do /
don't). The accessibility addon runs axe on every story in the a11y panel, and violations count as failures (`a11y.test: "error"`) when stories run as tests.

Primitives are shadcn/ui-style: Radix behaviour, restyled with the tokens, variants with `cva`.

| Component | Notes |
|---|---|
| `Button` | `primary` (teal), `secondary`, `ghost`, `danger` (critical), `ai` (violet); `md` / `sm` / `icon`; always ≥ target height |
| `Card`, `CardHeader`, `CardBody` | surface panels; header has title, description, actions |
| `Input`, `NativeSelect`, `Field` | native controls (keyboard and mobile behave as users expect); `Field` labels them |
| `Tabs` | Radix tabs: arrow keys, `aria-selected` |
| `Tip` | Radix tooltip; never the only place information lives |
| `Sheet` | Radix dialog as a side panel: focus trap, Esc, returns focus |
| `DataTable` | real `<table>` with caption and `scope` headers |
| `Notice` | advisory / warning / critical / gold, with icon and title |
| `Skeleton`, `EmptyState` | loading and empty |
| `RiskTierBadge` | icon + word + tier colour |
| `SeverityBadge` | S1–S6 in a gold hexagon (showstopper severity) |
| `GradeBadge` | G1–G4 in a petrol shield (evidence grade: human-verified, high-confidence extraction, pending review, inferred) |
| `RulesAIChip` | **R \| AI**: rules always; the AI half lit only when an LLM contributed |
| `EvidenceChip` | id + source label; kinds document / audio (sepia), query / aggregate (petrol), live (cyan); opens the source (`EvidenceViewer` in the app: page image with the value boxed in gold, audio span, SQL) |
| `FreshnessIndicator` | Live / Data stale / Data disconnected / No feed, with icon and age; `role=status` |
| `RigStatePill` | rig state in words (drilling rotary, tripping out, …) |
| `Quantity`, `Stat` | value + unit, mono, "–" when missing |
| `FormationSwatch` | earth-tone swatch + name |
| `ThemeToggle` | Office / Rig, `aria-pressed` |

## Charts, maps and documents

The data views follow the same tokens; they live in `apps/web/src/components` because they are
specific to the app.

| View | Built with | Rules |
|---|---|---|
| Time series, envelopes, correlation, replay strips | ECharts 6 on canvas (`Chart.tsx`) | Colours read from the theme's CSS variables at draw time and redrawn on a theme change. **Strokes use the meaning's text shade** (`petrol`, `cyan`, …), never `-solid`: petrol-solid on the Rig background is invisible. Fills may use `-solid`. Animation only when `motionOk()`. Axis labels mono 11 px, tooltips confined to the chart. ECharts' own aria is off: the container has a label and every chart has a table or text alternative next to it |
| Offset map | MapLibre 6 (basemap, camera, controls, keyboard) + deck.gl 9 (`MapboxOverlay`, layers) | Offline style (background only; `VITE_BASEMAP_STYLE` for a self-hosted one). deck's view has a deep far plane so subsurface paths are not clipped. Surface layers do not write depth, labels always draw on top. Labels: SDF with an outline for well names, bitmap on a chip below 12 px (SDF blurs) |
| Roadmap strip, mini map | SVG drawn at the measured pixel width (`useWidth`) | Never scaled by a viewBox, so 11 px text stays 11 px. Formation fills are the earth tones with fixed dark text (the tones are light in both themes) |
| Document pages | PDF.js 5 | Rendered at `devicePixelRatio`; the cited box is a gold outline offset from the text (a filled box hides thin table rows) |

Rig states are categories, not meanings: petrol at falling strength for the drilling family,
neutrals for the rest, and every segment labelled. Hazards in the replay studio are separated by
strip (one per hazard), not by colour.

## Do and don't

| Do | Don't |
|---|---|
| `<Quantity value={2153.1} unit="m MD" />` | `2153.1` with no unit, or the unit in a column header only |
| `<RiskTierBadge tier="critical" />` (icon + "Critical") | a red dot |
| Evidence chip next to every extracted number | a number with "from the WCR" in prose |
| `text-critical` for red body text | `text-critical-solid` (the brand red fails 4.5:1 on the Rig surface) |
| Grey the live panel **and** say "Data stale" | grey alone |
| **R \| AI** with the AI half dimmed when the rules wrote the text | an AI sparkle on rule output |
| Violet only for things an LLM wrote or will write | violet as an accent colour |
| One primary button per card | several teal buttons competing |
| Motion only on page entry and sheets, off in Rig | animated gauges, pulsing alerts on the rig screen |
| Hatch prior-only cells and say so in the description | the same fill for "no evidence" and "low risk" |

`stories/Patterns.stories.tsx` renders the first five pairs (risk, units, evidence, R | AI, stale) side by side.

## Accessibility (WCAG 2.2 AA)

- Contrast: text shades above meet 4.5:1 per theme; the Foundations story shows each shade on its theme and the a11y addon checks contrast.
- Keyboard: skip link to the page, visible focus ring (3 px, `focus` colour) on everything,
  Radix tabs / dialogs / tooltips, nav with `aria-current="page"`, the plan map's wells are links.
- Targets: ≥ 40 px Office, ≥ 48 px Rig (2.5.8 asks 24).
- Names: icon-only buttons have `aria-label`; badges and shields have `role=img` with a label;
  the 3D view has an `aria-label` and a table of the same content; the plan map has labels per well.
- Live regions: assistant answers (`aria-live=polite`), freshness (`role=status`), errors (`role=alert`).
- Reduced motion respected; Rig turns motion off entirely.

## Acceptance

| Check | How | Result |
|---|---|---|
| Storybook builds | `npm run build-storybook -w packages/ui` | builds (also in CI) |
| Web typechecks and builds | `npm run typecheck -w apps/web && npm run build -w apps/web` | clean |
| Lighthouse on the shell, mobile | `vite preview`, Lighthouse 12, Edge headless | performance 96, accessibility 100, best practices 100 |
| Lighthouse on the shell, desktop | same, `--preset=desktop` | performance 100, accessibility 100, best practices 100 |
| e2e | `PW_CHANNEL=msedge npx playwright test` in `tests/e2e` | 59 of 59 UI tests pass: main flows, the four data states of every screen, screenshots (API health needs the stack) |

The shell was audited signed out (no API). Heavy libraries are lazy chunks loaded by the screen
that needs them: MapLibre + deck.gl (offset map), ECharts (charts), PDF.js (evidence pages),
three.js (3D tab). The PWA precaches the app (6.6 MB, without the replay timelines) so the rig
screens work offline.
