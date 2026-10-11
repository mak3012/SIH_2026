import { Button, Card, CardBody, CardHeader, DataTable, FreshnessIndicator, Notice, Quantity, RiskTierBadge, RulesAIChip, Sheet } from "@repo/ui";
import { FileSearch, Mic, MicOff, Send, Sparkles } from "lucide-react";
import { useMemo, useRef, useState, type FormEvent } from "react";
import { api, ApiError, events } from "../api";
import { ErrorNote, PageHeader, SignInGate } from "../components/common";
import { Evidence, type Citation } from "../components/EvidenceViewer";
import { MiniMap } from "../components/MiniMap";
import { href } from "../router";
import { useApp, type Wellbore } from "../state";

type EvidenceRow = { id: string; kind: string; text: string; values?: Record<string, unknown>; query?: { view: string; sql: string; params: Record<string, unknown> } | null };
type Answer = {
  conversation_id: number;
  question: string;
  status: "answered" | "not_in_records" | "refused";
  answer: string;
  citations: Citation[];
  evidence: EvidenceRow[];
  followups: string[];
  refusal: string | null;
  drafter?: string | null;
  verification?: { verified?: boolean; checked?: number };
  transcript?: { text: string; language: string };
};
type Turn = { key: number; question: string; text: string; state: "thinking" | "streaming" | "done" | "error"; answer?: Answer; error?: unknown };

const SUGGESTED = [
  "What is the Barail top in SA-19?",
  "Which wells had losses in the Barail?",
  "What drilling problems were recorded in SA-07?",
  "Highest mud weight used in the Kopili?",
];

/** Wells the answer names, in the order it names them (a list answer becomes a table and a map). */
function wellsIn(text: string, wells: Wellbore[]): Wellbore[] {
  const hits = wells
    .map((w) => ({ w, at: text.search(new RegExp(`(?<![\\w-])${w.well.replace(/[-]/g, "[- ]?")}(?![\\w-])`, "i")) }))
    .filter((x) => x.at >= 0)
    .sort((a, b) => a.at - b.at);
  return hits.map((x) => x.w);
}

function EvidenceDrawer({ a }: { a: Answer }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button variant="secondary" size="sm" onClick={() => setOpen(true)}>
        <FileSearch /> Show evidence ({a.evidence.length})
      </Button>
      <Sheet open={open} onOpenChange={setOpen} wide title="Evidence behind this answer" description="Every record the answer could use, with the query that fetched it. Numbers in the answer were checked against these rows.">
        <ol className="m-0 grid list-none gap-3 p-0">
          {a.evidence.map((e) => {
            const c = a.citations.find((x) => x.id === e.id);
            return (
              <li key={e.id} className="rounded-md border border-line p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="num font-semibold">[{e.id}]</span>
                  <span className="text-sm text-muted">{e.kind}</span>
                  {c && <Evidence c={c} />}
                </div>
                <p className="mb-0 mt-1">{e.text}</p>
                {e.query && (
                  <details className="mt-2">
                    <summary className="cursor-pointer text-sm text-muted">Query on chat.{e.query.view}</summary>
                    <pre className="num mt-1 overflow-auto rounded-md bg-surface-2 p-2 text-xs">{e.query.sql}</pre>
                  </details>
                )}
              </li>
            );
          })}
        </ol>
      </Sheet>
    </>
  );
}

function WellsResult({ list, all }: { list: Wellbore[]; all: Wellbore[] }) {
  return (
    <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(0,420px)]" data-testid="wells-result">
      <DataTable
        caption={`${list.length} wells named in the answer`}
        head={["Well", "Block", "TD", "Feed", "Open alerts"]}
        rows={list.map((w) => [
          <a key="w" href={href("well", w.wellbore_id)} className="num font-semibold">
            {w.well}
          </a>,
          w.block,
          <Quantity key="td" value={w.td_md} unit="m MD" digits={0} />,
          <FreshnessIndicator key="f" state={(w.feed_state as "live") ?? "none"} />,
          w.open_alerts ? <RiskTierBadge key="a" tier="critical">{` ${w.open_alerts}`}</RiskTierBadge> : <span key="a" className="text-muted">none</span>,
        ])}
      />
      <MiniMap wells={all} highlight={new Set(list.map((w) => w.well))} height={280} label={`Map of the ${list.length} wells in the answer`} />
    </div>
  );
}

function TurnCard({ t, onFollow }: { t: Turn; onFollow: (q: string) => void }) {
  const { wells } = useApp();
  const a = t.answer;
  const list = useMemo(() => (a && wells ? wellsIn(a.answer, wells) : []), [a, wells]);
  return (
    <Card aria-busy={t.state !== "done" && t.state !== "error"}>
      <CardHeader
        title={a?.transcript ? `“${a.transcript.text}”` : t.question}
        description={a?.transcript ? `Spoken (${a.transcript.language})` : undefined}
        actions={a?.status === "answered" && <RulesAIChip ai={a.drafter === "llm"} />}
      />
      <CardBody className="grid gap-3">
        {t.state === "thinking" && <p className="m-0 text-muted">Searching the records and checking every number…</p>}
        {t.state === "error" && <ErrorNote error={t.error} />}
        {a?.status === "refused" ? (
          <Notice tone="warning" title="Not answered">
            {a.refusal ?? a.answer}
          </Notice>
        ) : (
          t.text && (
            <p data-testid="answer" className="m-0 whitespace-pre-wrap text-lg">
              {t.text}
              {t.state === "streaming" && <span aria-hidden className="ml-0.5 inline-block h-5 w-2 motion-safe:animate-pulse bg-fg align-middle" />}
            </p>
          )
        )}
        {a && a.status === "not_in_records" && <p className="m-0 text-sm text-muted">Nothing in the records you can access answers this; no guess is made.</p>}
        {a && list.length >= 2 && wells && <WellsResult list={list} all={wells} />}
        {a && a.citations.length > 0 && (
          <div data-testid="citations" className="flex flex-wrap gap-2">
            {a.citations.filter((c) => c.direct !== false).map((c) => (
              <Evidence key={c.id} c={c} />
            ))}
          </div>
        )}
        {a && a.evidence.length > 0 && (
          <div>
            <EvidenceDrawer a={a} />
          </div>
        )}
        {a && a.followups.length > 0 && (
          <div className="flex flex-wrap gap-2" aria-label="Suggested follow-ups">
            {a.followups.map((f) => (
              <Button key={f} variant="secondary" size="sm" onClick={() => onFollow(f)}>
                {f}
              </Button>
            ))}
          </div>
        )}
      </CardBody>
    </Card>
  );
}

function AskBody() {
  const [q, setQ] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [err, setErr] = useState<unknown>(null);
  const [rec, setRec] = useState<MediaRecorder | null>(null);
  const chunks = useRef<Blob[]>([]);
  const key = useRef(0);
  const conv = turns.find((t) => t.answer)?.answer?.conversation_id;
  const busy = turns.some((t) => t.state === "thinking" || t.state === "streaming");

  const update = (k: number, f: (t: Turn) => Turn) => setTurns((ts) => ts.map((t) => (t.key === k ? f(t) : t)));

  async function ask(question: string) {
    const k = ++key.current;
    setTurns((ts) => [{ key: k, question, text: "", state: "thinking" }, ...ts]);
    setQ("");
    try {
      for await (const { event, data } of events("/assistant/ask/stream", { question, conversation_id: conv ?? null })) {
        if (event === "delta") update(k, (t) => ({ ...t, state: "streaming", text: t.text + data.text }));
        else if (event === "done") update(k, (t) => ({ ...t, state: "done", text: data.answer, answer: data }));
        else if (event === "error") update(k, (t) => ({ ...t, state: "error", error: new ApiError(data.status, data.detail) }));
      }
    } catch (e) {
      update(k, (t) => ({ ...t, state: "error", error: e }));
    }
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    if (q.trim() && !busy) void ask(q.trim());
  }

  async function voice() {
    if (rec) {
      rec.stop();
      return;
    }
    setErr(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const r = new MediaRecorder(stream);
      chunks.current = [];
      r.ondataavailable = (e) => chunks.current.push(e.data);
      r.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        setRec(null);
        const k = ++key.current;
        setTurns((ts) => [{ key: k, question: "Spoken question", text: "", state: "thinking" }, ...ts]);
        const blob = new Blob(chunks.current, { type: r.mimeType });
        api<Answer>(`/assistant/ask/voice${conv ? `?conversation_id=${conv}` : ""}`, { method: "POST", body: blob, contentType: r.mimeType }).then(
          (a) => update(k, (t) => ({ ...t, question: a.question, state: "done", text: a.answer, answer: a })),
          (e) => update(k, (t) => ({ ...t, state: "error", error: e })),
        );
      };
      r.start();
      setRec(r);
    } catch (e) {
      setErr(e);
    }
  }

  return (
    <div className="grid gap-4">
      <Card>
        <CardBody>
          <form onSubmit={submit} className="flex flex-wrap items-end gap-2">
            <label className="grid min-w-[260px] flex-1 gap-1 text-sm">
              Question
              <input
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="e.g. Which wells had losses in the Barail?"
                className="min-h-target rounded-md border border-line-strong bg-surface px-3 text-base text-fg"
              />
            </label>
            <Button type="submit" disabled={busy || !q.trim()}>
              <Send /> {busy ? "Answering…" : "Ask"}
            </Button>
            <Button type="button" variant="secondary" onClick={voice} disabled={busy && !rec} aria-pressed={!!rec} aria-label={rec ? "Stop recording" : "Speak a question"}>
              {rec ? <MicOff /> : <Mic />} {rec ? "Stop" : "Voice"}
            </Button>
          </form>
          <div className="mt-3 flex flex-wrap items-center gap-2" aria-label="Suggested questions">
            <Sparkles aria-hidden className="size-4 text-muted" />
            {SUGGESTED.map((x) => (
              <Button key={x} variant="ghost" size="sm" disabled={busy} onClick={() => void ask(x)}>
                {x}
              </Button>
            ))}
          </div>
        </CardBody>
      </Card>
      <ErrorNote error={err} />
      {turns.length === 0 && (
        <p className="m-0 text-muted">
          Answers come only from the records your licences cover. Numbers are checked against the evidence before the answer is shown; a question the records cannot answer is
          declined, not guessed.
        </p>
      )}
      <div aria-live="polite" className="grid gap-4">
        {turns.map((t) => (
          <TurnCard key={t.key} t={t} onFollow={(f) => void ask(f)} />
        ))}
      </div>
    </div>
  );
}

export function AskPage() {
  return (
    <>
      <PageHeader title="Ask WellMind" description="Questions about your wells, answered from the records only. Every number links to the page, voice note or query it came from." />
      <SignInGate>
        <AskBody />
      </SignInGate>
    </>
  );
}
