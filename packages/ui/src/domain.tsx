import {
  Activity,
  ArrowDownToLine,
  ArrowUpFromLine,
  CircleSlash,
  Database,
  Drill,
  FileText,
  HardHat,
  Info,
  Link2,
  Mic,
  OctagonAlert,
  Pause,
  RefreshCw,
  RotateCw,
  Sigma,
  Sun,
  TriangleAlert,
  Waves,
  WifiOff,
} from "lucide-react";
import type { ComponentPropsWithoutRef, ReactNode } from "react";
import { cn } from "./cn";
import { useTheme } from "./theme";

// ------------------------------------------------------------------ risk tiers

export type Tier = "advisory" | "warning" | "critical";
const TIER = {
  advisory: { icon: Info, label: "Advisory", cls: "border-advisory text-advisory bg-advisory-subtle" },
  warning: { icon: TriangleAlert, label: "Warning", cls: "border-warning text-warning bg-warning-subtle" },
  critical: { icon: OctagonAlert, label: "Critical", cls: "border-critical text-critical bg-critical-subtle" },
} as const;

export const tierOf = (n: number): Tier => (n >= 3 ? "critical" : n === 2 ? "warning" : "advisory");

/** Risk tier: icon + word + colour, so colour is never the only signal. */
export function RiskTierBadge({ tier, className, children }: { tier: Tier; className?: string; children?: ReactNode }) {
  const t = TIER[tier];
  const Icon = t.icon;
  return (
    <span className={cn("inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-sm font-semibold", t.cls, className)}>
      <Icon aria-hidden className="size-4" />
      {t.label}
      {children}
    </span>
  );
}

// ------------------------------------------------------------------ S1-S6 / G1-G4

/** Severity S1 (minor) ... S5 (well-control event), S6 (showstopper: stop operations). Gold hexagon. */
export function SeverityBadge({ level, className }: { level: 1 | 2 | 3 | 4 | 5 | 6; className?: string }) {
  const names = ["", "minor", "moderate", "serious", "major", "well control", "showstopper"];
  return (
    <span className={cn("inline-flex items-center gap-1 text-sm", className)} title={`Severity S${level}: ${names[level]}`}>
      <svg viewBox="0 0 28 32" className="h-7 w-6" role="img" aria-label={`Severity S${level}, ${names[level]}`}>
        <path d="M14 1 27 8.5v15L14 31 1 23.5v-15Z" fill={level >= 5 ? "var(--gold-solid)" : "var(--gold-subtle)"} stroke="var(--gold-solid)" strokeWidth="2" />
        <text x="14" y="20.5" textAnchor="middle" fontSize="11" fontWeight="700" fontFamily="IBM Plex Mono, monospace" fill={level >= 5 ? "#1a1300" : "var(--gold)"}>
          S{level}
        </text>
      </svg>
    </span>
  );
}

/** Evidence grade, petrol shield: G1 human-verified, G2 high-confidence extraction,
 *  G3 pending review / low confidence, G4 inferred or unknown provenance. */
export function GradeBadge({ grade, className }: { grade: 1 | 2 | 3 | 4; className?: string }) {
  const names = ["", "human-verified", "high-confidence extraction", "pending review", "inferred / unknown"];
  return (
    <span className={cn("inline-flex items-center", className)} title={`Evidence grade G${grade}: ${names[grade]}`}>
      <svg viewBox="0 0 28 32" className="h-7 w-6" role="img" aria-label={`Evidence grade G${grade}, ${names[grade]}`}>
        <path d="M14 1 26 5v10c0 8-5.4 13.2-12 16C7.4 28.2 2 23 2 15V5Z" fill={grade === 1 ? "var(--petrol-solid)" : "var(--petrol-subtle)"} stroke="var(--petrol-solid)" strokeWidth="2" strokeDasharray={grade === 4 ? "3 2" : undefined} />
        <text x="14" y="20" textAnchor="middle" fontSize="11" fontWeight="700" fontFamily="IBM Plex Mono, monospace" fill={grade === 1 ? "#ffffff" : "var(--petrol)"}>
          G{grade}
        </text>
      </svg>
    </span>
  );
}

/** Grade from a citation's provenance. */
export function gradeOf(review?: string | null, confidence?: number | null, inferred?: boolean): 1 | 2 | 3 | 4 {
  if (inferred) return 4;
  if (review === "accepted" || review === "corrected") return 1;
  if (review === "pending") return 3;
  return confidence != null && confidence >= 0.9 ? 2 : 3;
}

// ------------------------------------------------------------------ R | AI

/** Where rules decide and AI explains: "R" always, "AI" lit when model text is shown. */
export function RulesAIChip({ ai = true, className }: { ai?: boolean; className?: string }) {
  return (
    <span
      className={cn("inline-flex overflow-hidden rounded-full border border-line-strong font-mono text-xs font-semibold leading-6", className)}
      title={ai ? "Rules decided; AI wrote the explanation (checked by rules)" : "Decided by rules; no AI text"}
      aria-label={ai ? "Rules decided, AI explained" : "Rules only"}
    >
      <span className="bg-petrol-subtle px-2 text-petrol">R</span>
      <span className={cn("border-l border-line-strong px-2", ai ? "bg-violet-subtle text-violet" : "text-muted line-through")}>AI</span>
    </span>
  );
}

// ------------------------------------------------------------------ evidence chips

export type EvidenceKind = "document" | "audio" | "query" | "aggregate" | "live";
const EV = {
  document: { icon: FileText, cls: "border-sepia text-sepia bg-sepia-subtle" },
  audio: { icon: Mic, cls: "border-sepia text-sepia bg-sepia-subtle" },
  query: { icon: Database, cls: "border-petrol text-petrol bg-petrol-subtle" },
  aggregate: { icon: Sigma, cls: "border-petrol text-petrol bg-petrol-subtle" },
  live: { icon: Activity, cls: "border-cyan text-cyan bg-cyan-subtle" },
} as const;

/** A citation: id, what it points to; click opens the source (page with box, audio span). */
export function EvidenceChip({ id, kind, label, className, ...p }: { id: string; kind: EvidenceKind; label: string } & ComponentPropsWithoutRef<"button">) {
  const e = EV[kind];
  const Icon = e.icon;
  return (
    <button
      type="button"
      className={cn(
        "inline-flex min-h-[28px] max-w-full items-center gap-1.5 rounded-md border px-2 font-mono text-xs " +
          "hover:brightness-95 focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-focus",
        e.cls,
        className,
      )}
      aria-label={`Evidence ${id}: ${label}. Open source.`}
      {...p}
    >
      <Icon aria-hidden className="size-3.5 shrink-0" />
      <span className="font-semibold">{id}</span>
      <span className="truncate">{label}</span>
      <Link2 aria-hidden className="size-3 shrink-0 opacity-70" />
    </button>
  );
}

// ------------------------------------------------------------------ live data

export type FeedState = "live" | "stale" | "disconnected" | "stopped" | "none";

/** Data freshness: cyan when live; grey with an icon and the age when not. */
export function FreshnessIndicator({ state, age, className }: { state: FeedState; age?: string; className?: string }) {
  const live = state === "live";
  const Icon = live ? Activity : state === "disconnected" ? WifiOff : state === "none" ? CircleSlash : Pause;
  const label = live ? "Live" : state === "none" ? "No feed" : `Data ${state}`;
  return (
    <span
      role="status"
      data-state={state}
      className={cn(
        "inline-flex min-h-[32px] items-center gap-1.5 rounded-full border px-3 text-sm font-semibold",
        live ? "border-cyan bg-cyan-subtle text-cyan" : "border-line-strong bg-surface-2 text-muted",
        className,
      )}
    >
      <Icon aria-hidden className="size-4" />
      {label}
      {age && <span className="font-mono text-xs font-normal">{age}</span>}
    </span>
  );
}

const RIG = {
  drilling_rotary: { icon: Drill, label: "Drilling · rotary" },
  drilling_slide: { icon: Drill, label: "Drilling · slide" },
  reaming: { icon: RotateCw, label: "Reaming" },
  circulating: { icon: RefreshCw, label: "Circulating" },
  tripping_in: { icon: ArrowDownToLine, label: "Tripping in" },
  tripping_out: { icon: ArrowUpFromLine, label: "Tripping out" },
  connection: { icon: Waves, label: "Connection" },
  static: { icon: Pause, label: "Static" },
  unknown: { icon: CircleSlash, label: "Rig state unknown" },
} as const;

export function RigStatePill({ state, className }: { state: string | null | undefined; className?: string }) {
  const r = RIG[(state ?? "unknown") as keyof typeof RIG] ?? RIG.unknown;
  const Icon = r.icon;
  return (
    <span className={cn("inline-flex min-h-[32px] items-center gap-1.5 rounded-full border border-line-strong bg-surface px-3 text-sm font-medium", className)}>
      <Icon aria-hidden className="size-4 text-cyan" />
      {r.label}
    </span>
  );
}

// ------------------------------------------------------------------ numbers with units

/** A number always with its unit, monospaced; "–" when missing. */
export function Quantity({ value, unit, digits = 1, className }: { value: number | null | undefined; unit: string; digits?: number; className?: string }) {
  if (value == null || Number.isNaN(value)) return <span className={cn("num text-muted", className)}>–</span>;
  const n = value.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return (
    <span className={cn("num whitespace-nowrap", className)}>
      {n}
      <span className="text-muted">{" "}{unit}</span>
    </span>
  );
}

export function Stat({ label, children, note, accent = "fg" }: { label: string; children: ReactNode; note?: ReactNode; accent?: "fg" | "petrol" | "sepia" | "cyan" | "violet" | "gold" }) {
  const color = { fg: "text-fg", petrol: "text-petrol", sepia: "text-sepia", cyan: "text-cyan", violet: "text-violet", gold: "text-gold" }[accent];
  return (
    <div className="min-w-[160px]">
      <div className="text-sm text-muted">{label}</div>
      <div className={cn("text-2xl font-semibold leading-tight", color)}>{children}</div>
      {note && <div className="mt-0.5 text-xs text-muted">{note}</div>}
    </div>
  );
}

// ------------------------------------------------------------------ theme toggle

export function ThemeToggle({ className }: { className?: string }) {
  const [theme, setTheme] = useTheme();
  const rig = theme === "rig";
  return (
    <button
      type="button"
      onClick={() => setTheme(rig ? "office" : "rig")}
      aria-pressed={rig}
      className={cn(
        "inline-flex min-h-target items-center gap-2 rounded-md border border-line-strong px-3 text-sm font-medium " +
          "hover:bg-surface-2 focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-focus",
        className,
      )}
    >
      {rig ? <HardHat aria-hidden className="size-4" /> : <Sun aria-hidden className="size-4" />}
      {rig ? "Rig" : "Office"}
      <span className="sr-only">theme; switch to {rig ? "Office" : "Rig"}</span>
    </button>
  );
}

/** Formation colour by order (earth tones), with the name: never colour alone. */
export function FormationSwatch({ name, index }: { name: string; index: number }) {
  const tones = ["bg-earth-1", "bg-earth-2", "bg-earth-3"];
  return (
    <span className="inline-flex items-center gap-1.5 text-sm">
      <span aria-hidden className={cn("inline-block size-3 rounded-sm border border-line-strong", tones[index % 3])} />
      {name}
    </span>
  );
}
