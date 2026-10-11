import { Button, Card, CardBody, CardHeader, cn, Field, GradeBadge, gradeOf, Input, NativeSelect, Notice } from "@repo/ui";
import { Check, Keyboard, PencilLine, TriangleAlert, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { can } from "../auth";
import { DataState, ErrorNote, PageHeader, pct, SignInGate, useLoad, words } from "../components/common";
import { AudioSpan, PdfPage } from "../components/EvidenceViewer";
import { useApp } from "../state";

export type Item = {
  id: number;
  kind: string;
  doc_type: string | null;
  field: string;
  value: unknown;
  printed: string | null;
  page: number | null;
  bbox: number[] | null;
  t_start: number | null;
  t_end: number | null;
  confidence: number;
  components: Record<string, number> | null;
  rule_failures: string[] | null;
  reason: string | null;
  status: string;
  document_id: number | null;
  title: string | null;
  audio_note_id: number | null;
  value_score: number;
  conflict: boolean;
};

const show = (v: unknown) => (v == null ? "–" : typeof v === "object" ? JSON.stringify(v) : String(v));
const norm = (v: unknown) => show(v).replace(/[\s,]/g, "").toLowerCase();

/** Why this item needs a human: each signal that fell short, worst first. */
function conflicts(i: Item): { text: string; strong: boolean }[] {
  const out: { text: string; strong: boolean }[] = [];
  for (const f of i.rule_failures ?? []) out.push({ text: f, strong: true });
  const c = i.components ?? {};
  if (c.consistency === 0) out.push({ text: "the two extraction passes disagree", strong: true });
  if (i.printed && i.value != null && !norm(i.printed).includes(norm(i.value)) && !norm(i.value).includes(norm(i.printed)))
    out.push({ text: `printed “${i.printed}” does not read as ${show(i.value)}`, strong: true });
  for (const [k, v] of Object.entries(c)) if (v < 0.8 && !(k === "consistency" && v === 0)) out.push({ text: `${k} confidence ${pct(v)}`, strong: false });
  return out;
}

function Detail({ item, write, onDecide, valueRef }: { item: Item; write: boolean; onDecide: (action: "accept" | "correct" | "reject", value?: unknown) => void; valueRef: React.RefObject<HTMLInputElement | null> }) {
  const [value, setValue] = useState(show(item.value));
  useEffect(() => setValue(show(item.value)), [item]);
  const cs = conflicts(item);
  const open = item.status === "open";
  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_380px]">
      <div>
        {item.document_id && item.page ? (
          <PdfPage documentId={item.document_id} page={item.page} bbox={item.bbox} label={`${item.title}, page ${item.page}, the extracted value boxed`} />
        ) : item.audio_note_id ? (
          <AudioSpan noteId={item.audio_note_id} start={item.t_start ?? 0} end={item.t_end ?? (item.t_start ?? 0) + 5} />
        ) : (
          <Notice tone="advisory">No page or recording is linked to this item.</Notice>
        )}
      </div>
      <div className="grid content-start gap-4">
        <dl className="m-0 grid grid-cols-[110px_1fr] gap-x-3 gap-y-2" data-testid="review-fields">
          <dt className="text-muted">Field</dt>
          <dd className="m-0 font-semibold">{words(item.field)}</dd>
          <dt className="text-muted">Extracted</dt>
          <dd className={cn("num m-0 rounded px-1 text-lg", item.conflict && "bg-critical-subtle text-critical")}>{show(item.value)}</dd>
          <dt className="text-muted">Printed</dt>
          <dd className="num m-0">{item.printed ?? "–"}</dd>
          <dt className="text-muted">Source</dt>
          <dd className="m-0">
            {item.title ?? (item.audio_note_id ? `voice note ${item.audio_note_id}` : item.kind)}
            {item.page ? `, p. ${item.page}` : ""}
          </dd>
          <dt className="text-muted">Confidence</dt>
          <dd className="m-0">
            <span className="num">{pct(item.confidence)}</span>
            <span className="ml-2 inline-flex flex-wrap gap-2 align-middle">
              {Object.entries(item.components ?? {}).map(([k, v]) => (
                <span key={k} className={cn("num rounded border px-1.5 text-xs", v < 0.8 ? "border-warning-solid text-warning" : "border-line text-muted")}>
                  {k} {v.toFixed(2)}
                </span>
              ))}
            </span>
          </dd>
        </dl>
        {cs.length > 0 && (
          <div className="rounded-md border border-critical-solid/50 bg-critical-subtle p-3" data-testid="review-conflicts">
            <h3 className="m-0 flex items-center gap-2 text-sm font-semibold text-critical">
              <TriangleAlert aria-hidden className="size-4" /> Held because
            </h3>
            <ul className="mb-0 mt-1 pl-5 text-sm">
              {cs.map((c, k) => (
                <li key={k} className={c.strong ? "font-semibold" : ""}>
                  {c.text}
                </li>
              ))}
            </ul>
          </div>
        )}
        {write && open ? (
          <form
            className="grid gap-3"
            onSubmit={(e) => {
              e.preventDefault();
              const v = Number.isFinite(Number(value)) && value.trim() !== "" ? Number(value) : value;
              onDecide("correct", v);
            }}
          >
            <Field label="Correct value (C, then Enter)">
              <Input ref={valueRef} value={value} onChange={(e) => setValue(e.target.value)} className="num" />
            </Field>
            <div className="flex flex-wrap gap-2">
              <Button type="button" onClick={() => onDecide("accept")}>
                <Check /> Accept <kbd className="num ml-1 rounded border border-white/40 px-1 text-xs">A</kbd>
              </Button>
              <Button type="submit" variant="secondary" disabled={value === show(item.value)}>
                <PencilLine /> Correct
              </Button>
              <Button type="button" variant="danger" onClick={() => onDecide("reject")}>
                <X /> Reject <kbd className="num ml-1 rounded border border-white/40 px-1 text-xs">R</kbd>
              </Button>
            </div>
          </form>
        ) : (
          <p className="m-0 text-muted">{open ? "Deciding needs the data steward role." : `Already ${item.status}.`}</p>
        )}
      </div>
    </div>
  );
}

function Queue({ items, current, onPick }: { items: Item[]; current: number; onPick: (i: number) => void }) {
  const max = Math.max(...items.map((i) => i.value_score), 0.01);
  return (
    <ol className="m-0 list-none p-0" aria-label="Review queue, highest value first">
      {items.map((i, k) => (
        <li key={i.id}>
          <button
            type="button"
            onClick={() => onPick(k)}
            aria-current={k === current}
            data-testid={`queue-${i.id}`}
            className={cn("grid min-h-target w-full grid-cols-[auto_1fr_64px] items-center gap-3 border-b border-line px-3 py-2 text-left hover:bg-surface-2", k === current && "bg-teal-subtle outline outline-2 -outline-offset-2 outline-[var(--teal-solid)]")}
          >
            <GradeBadge grade={gradeOf(i.status === "open" ? "pending" : i.status, i.confidence)} />
            <span className="min-w-0">
              <span className="flex items-center gap-1.5 font-medium">
                {i.conflict && <TriangleAlert aria-label="conflict" className="size-4 shrink-0 text-critical" />}
                <span className="truncate">{words(i.field)}</span>
              </span>
              <span className="block truncate text-xs text-muted">
                {i.title ?? (i.audio_note_id ? `voice note ${i.audio_note_id}` : i.kind)}
                {i.page ? ` · p. ${i.page}` : ""} · {i.status}
              </span>
            </span>
            <span title={`value ${i.value_score}`}>
              <span className="block h-1.5 rounded-sm bg-surface-2" aria-hidden>
                <span className="block h-1.5 rounded-sm bg-gold-solid" style={{ width: `${(i.value_score / max) * 100}%` }} />
              </span>
              <span className="num block text-right text-xs">{i.value_score.toFixed(2)}</span>
            </span>
          </button>
        </li>
      ))}
    </ol>
  );
}

function ReviewBody() {
  const { user } = useApp();
  const [status, setStatus] = useState("open");
  const state = useLoad<Item[]>(`/review/items?status=${status}&order=value&limit=200`);
  const [local, setLocal] = useState<Record<number, string>>({});
  const items = useMemo(() => (state.data ?? []).map((i) => (local[i.id] ? { ...i, status: local[i.id] } : i)), [state.data, local]);
  const [cur, setCur] = useState(0);
  const [said, setSaid] = useState("");
  const [err, setErr] = useState<unknown>(null);
  const valueRef = useRef<HTMLInputElement>(null);
  const write = can(user, "review");
  const item = items[Math.min(cur, items.length - 1)];

  const decide = useCallback(
    async (action: "accept" | "correct" | "reject", value?: unknown) => {
      if (!item || item.status !== "open" || !write) return;
      setErr(null);
      try {
        await api(`/review/items/${item.id}`, { json: { action, value: action === "correct" ? value : undefined } });
        const st = { accept: "accepted", correct: "corrected", reject: "rejected" }[action];
        setLocal((l) => ({ ...l, [item.id]: st }));
        setSaid(`${words(item.field)} ${st}.`);
        const next = items.findIndex((x, k) => k > cur && x.status === "open" && !local[x.id]);
        if (next >= 0) setCur(next);
      } catch (e) {
        setErr(e);
      }
    },
    [item, items, cur, local, write],
  );

  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (e.ctrlKey || e.metaKey || e.altKey || ["INPUT", "SELECT", "TEXTAREA"].includes(t.tagName) || t.isContentEditable) return;
      if (document.querySelector('[role="dialog"]')) return;
      const k = e.key.toLowerCase();
      if (k === "j") setCur((c) => Math.min(c + 1, items.length - 1));
      else if (k === "k") setCur((c) => Math.max(c - 1, 0));
      else if (k === "a") void decide("accept");
      else if (k === "r") void decide("reject");
      else if (k === "c") valueRef.current?.focus();
      else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, [items.length, decide]);

  const open = items.filter((i) => i.status === "open").length;
  return (
    <>
      <div className="mb-4 flex flex-wrap items-end gap-4">
        <Field label="Show">
          <NativeSelect value={status} onChange={(e) => setStatus(e.target.value)}>
            {["open", "accepted", "corrected", "rejected", "all"].map((s) => (
              <option key={s}>{s}</option>
            ))}
          </NativeSelect>
        </Field>
        <p className="m-0 flex items-center gap-2 pb-2 text-sm text-muted">
          <Keyboard aria-hidden className="size-4" />
          <kbd className="num rounded border border-line-strong px-1">J</kbd> next · <kbd className="num rounded border border-line-strong px-1">K</kbd> previous ·{" "}
          <kbd className="num rounded border border-line-strong px-1">A</kbd> accept · <kbd className="num rounded border border-line-strong px-1">R</kbd> reject ·{" "}
          <kbd className="num rounded border border-line-strong px-1">C</kbd> correct
        </p>
        <span role="status" aria-live="polite" className="ml-auto pb-2 text-sm font-semibold text-teal" data-testid="review-said">
          {said}
        </span>
      </div>
      {!write && <Notice tone="advisory">Deciding needs the data steward role; you can browse the queue.</Notice>}
      <ErrorNote error={err} />
      <DataState state={state} skeleton="h-[600px]" isEmpty={(d) => d.length === 0} empty={{ title: status === "open" ? "Nothing waiting for review" : `No ${status} items`, body: "Extraction holds a value here when it is not sure of it." }}>
        {() => (
          <div className="grid gap-4 xl:grid-cols-[360px_minmax(0,1fr)]">
            <Card>
              <CardHeader title={`Queue · ${open} open`} description="Highest value first: field importance × doubt × failed rules." />
              <CardBody className="max-h-[72vh] overflow-auto p-0">
                <Queue items={items} current={cur} onPick={setCur} />
              </CardBody>
            </Card>
            {item && (
              <Card>
                <CardHeader title={`${words(item.field)} · #${item.id}`} description={`${item.kind}${item.doc_type ? ` · ${item.doc_type}` : ""} · ${item.status} · value ${item.value_score.toFixed(2)}`} />
                <CardBody>
                  <Detail item={item} write={write} onDecide={decide} valueRef={valueRef} />
                </CardBody>
              </Card>
            )}
          </div>
        )}
      </DataState>
    </>
  );
}

export function ReviewPage() {
  return (
    <>
      <PageHeader title="Review console" description="Extracted values the pipeline would not trust on its own, the most valuable first. Each decision is audited and applied on the next pipeline run." />
      <SignInGate>
        <ReviewBody />
      </SignInGate>
    </>
  );
}
