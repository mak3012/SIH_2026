import type { Meta, StoryObj } from "@storybook/react-vite";
import { brand, earth, fontSize, motion, radius, risk, slate, space, target, themes } from "../tokens";

const meta: Meta = { title: "Foundations/Tokens" };
export default meta;

function Swatch({ name, value, note }: { name: string; value: string; note?: string }) {
  return (
    <div className="flex items-center gap-3 py-1">
      <span aria-hidden className="h-10 w-16 rounded-md border border-line" style={{ background: value }} />
      <div>
        <div className="font-semibold">{name}</div>
        <div className="num text-sm text-muted">
          {value}
          {note ? ` · ${note}` : ""}
        </div>
      </div>
    </div>
  );
}

export const Colours: StoryObj = {
  render: () => (
    <div className="grid gap-8 md:grid-cols-3">
      <section>
        <h3>Meaning</h3>
        <Swatch name="petrol" value={brand.petrol} note="memory / data" />
        <Swatch name="sepia" value={brand.sepia} note="documents / history" />
        <Swatch name="cyan" value={brand.cyan} note="live / eRTMAC" />
        <Swatch name="violet" value={brand.violet} note="AI" />
        <Swatch name="teal" value={brand.teal} note="people / UI actions" />
        <Swatch name="gold" value={brand.gold} note="showstoppers / evidence" />
      </section>
      <section>
        <h3>Risk tiers</h3>
        <Swatch name="advisory" value={risk.advisory} />
        <Swatch name="warning" value={risk.warning} />
        <Swatch name="critical" value={risk.critical} />
        <h3>Formations</h3>
        {earth.map((e, i) => (
          <Swatch key={e} name={`earth ${i + 1}`} value={e} />
        ))}
      </section>
      <section>
        <h3>Slate</h3>
        {Object.entries(slate).map(([k, v]) => (
          <Swatch key={k} name={`slate ${k}`} value={v} />
        ))}
      </section>
    </div>
  ),
};

export const AccessibleText: StoryObj = {
  name: "Text shades per theme (AA)",
  render: () => (
    <div className="grid gap-6 md:grid-cols-2">
      {(["office", "rig"] as const).map((t) => (
        <div key={t} className="rounded-lg p-4" style={{ background: themes[t].surface, color: themes[t].text }}>
          <h3 className="mt-0">{themes[t].label}</h3>
          {Object.entries(themes[t].text_).map(([k, v]) => (
            <p key={k} className="m-0 py-0.5" style={{ color: v }}>
              {k}: body text in {v}
            </p>
          ))}
        </div>
      ))}
    </div>
  ),
};

export const Type: StoryObj = {
  render: () => (
    <div className="grid gap-3">
      {Object.entries(fontSize).map(([k, v]) => (
        <div key={k} className="flex items-baseline gap-4">
          <span className="num w-16 text-sm text-muted">{k}</span>
          <span style={{ fontSize: v }}>Barail top 2,153.1 m MD</span>
        </div>
      ))}
      <p className="num">IBM Plex Mono for depths, units, IDs and citations: 2,153.1 m MD · 1.28 sg · E12 · SA-19</p>
    </div>
  ),
};

export const SpacingRadiusMotion: StoryObj = {
  name: "Spacing, radius, targets, motion",
  render: () => (
    <div className="grid gap-4">
      <div className="flex items-end gap-2">
        {Object.entries(space).map(([k, v]) => (
          <div key={k} className="text-center">
            <div className="bg-teal-solid" style={{ width: 16, height: v || 1 }} />
            <span className="num text-xs">{k}</span>
          </div>
        ))}
      </div>
      <p className="m-0">
        Radius: <span className="num">{JSON.stringify(radius)}</span> · Touch targets:{" "}
        <span className="num">Office {target.office} px, Rig {target.rig} px</span> · Motion:{" "}
        <span className="num">{motion.fast * 1000}–{motion.slow * 1000} ms</span>, none on the Rig screen
      </p>
    </div>
  ),
};
