import { Button, cn, EvidenceChip, gradeOf, GradeBadge, Notice, Sheet, Skeleton, type EvidenceKind } from "@repo/ui";
import { ChevronLeft, ChevronRight, Pause, Play } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, API, ApiError } from "../api";
import { token as authToken } from "../auth";
import { pct, words } from "./common";

export type Citation = {
  id: string;
  kind?: string;
  type?: string;
  direct?: boolean;
  document?: string;
  document_id?: number;
  page?: number;
  bbox?: number[] | null;
  audio_note_id?: number;
  uri?: string;
  t_start?: number;
  t_end?: number;
  view?: string;
  sql?: string;
  derived_from?: string[];
  text?: string;
  review_status?: string;
  synthetic?: boolean;
  source_ref_id?: number;
};

export function kindOf(c: Citation): EvidenceKind {
  if (c.type === "document") return "document";
  if (c.type === "audio") return "audio";
  if (c.type === "aggregate") return "aggregate";
  return "query";
}

export function labelOf(c: Citation): string {
  if (c.type === "document") return `${c.document} p. ${c.page}`;
  if (c.type === "audio") return `${c.uri?.split("/").pop()} ${c.t_start}–${c.t_end} s`;
  if (c.type === "aggregate") return `from ${c.derived_from?.join(", ")}`;
  return c.view ? `chat.${c.view}` : (c.kind ?? "record");
}

// ------------------------------------------------------------------ files

const files = new Map<string, Promise<ArrayBuffer>>();

/** An authenticated binary GET, cached for the session (PDFs and recordings do not change). */
export function fileOf(path: string): Promise<ArrayBuffer> {
  let p = files.get(path);
  if (!p) {
    p = fetch(`${API}${path}`, { headers: { Authorization: `Bearer ${authToken() ?? ""}` } }).then(async (r) => {
      if (!r.ok) {
        const detail = await r.json().then((j) => j.detail as string, () => r.statusText);
        throw new ApiError(r.status, detail);
      }
      return r.arrayBuffer();
    });
    p.catch(() => files.delete(path));
    files.set(path, p);
  }
  return p;
}

type PdfDoc = import("pdfjs-dist").PDFDocumentProxy;
const docs = new Map<number, Promise<PdfDoc>>();

async function pdfOf(documentId: number): Promise<PdfDoc> {
  let p = docs.get(documentId);
  if (!p) {
    p = (async () => {
      const [pdfjs, worker, data] = await Promise.all([
        import("pdfjs-dist"),
        import("pdfjs-dist/build/pdf.worker.min.mjs?url"),
        fileOf(`/documents/${documentId}/file`),
      ]);
      pdfjs.GlobalWorkerOptions.workerSrc = worker.default;
      return pdfjs.getDocument({ data: new Uint8Array(data.slice(0)) }).promise;
    })();
    p.catch(() => docs.delete(documentId));
    docs.set(documentId, p);
  }
  return p;
}

/**
 * One PDF page rendered by PDF.js at the screen's pixel density (crisp at any zoom), with the
 * cited box in gold and, optionally, other boxes on the page outlined.
 */
export function PdfPage({
  documentId,
  page,
  bbox,
  others = [],
  onPages,
  label,
}: {
  documentId: number;
  page: number;
  bbox?: number[] | null;
  others?: { bbox: number[]; label: string }[];
  onPages?: (n: number) => void;
  label: string;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const wrap = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<"loading" | "ready" | { error: string }>("loading");
  const [aspect, setAspect] = useState(1.414);

  useEffect(() => {
    let cancelled = false;
    let task: { cancel: () => void } | null = null;
    setState("loading");
    pdfOf(documentId)
      .then(async (doc) => {
        onPages?.(doc.numPages);
        const p = await doc.getPage(Math.min(Math.max(1, page), doc.numPages));
        const width = wrap.current?.clientWidth || 700;
        const base = p.getViewport({ scale: 1 });
        const vp = p.getViewport({ scale: width / base.width });
        const dpr = window.devicePixelRatio || 1;
        const c = canvas.current;
        if (!c || cancelled) return;
        c.width = Math.floor(vp.width * dpr);
        c.height = Math.floor(vp.height * dpr);
        c.style.width = `${vp.width}px`;
        c.style.height = `${vp.height}px`;
        setAspect(vp.height / vp.width);
        const t = p.render({ canvas: c, viewport: vp, transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : undefined });
        task = t;
        await t.promise;
        if (!cancelled) setState("ready");
      })
      .catch((e: unknown) => {
        if (cancelled || (e as { name?: string })?.name === "RenderingCancelledException") return;
        setState({
          error:
            e instanceof ApiError && e.status === 404
              ? "No stored file for this document: the extracted values are listed beside it."
              : `Could not show the page (${e instanceof Error ? e.message : String(e)}).`,
        });
      });
    return () => {
      cancelled = true;
      task?.cancel();
    };
  }, [documentId, page, onPages]);

  const box = (b: number[], cls: string, key: string, title?: string) => (
    <span key={key} title={title} aria-hidden className={cls} style={{ left: `${b[0] * 100}%`, top: `${b[1] * 100}%`, width: `${(b[2] - b[0]) * 100}%`, height: `${(b[3] - b[1]) * 100}%` }} />
  );

  if (typeof state === "object") return <Notice tone="advisory">{state.error}</Notice>;
  return (
    <figure ref={wrap} className="relative m-0 overflow-hidden rounded-md border border-line bg-white" style={{ minHeight: state === "loading" ? 320 : undefined }} data-testid="pdf-page">
      {state === "loading" && <Skeleton className="absolute inset-0" />}
      <canvas ref={canvas} role="img" aria-label={label} className="block max-w-full" />
      {state === "ready" && others.map((o, i) => box(o.bbox, "absolute rounded-sm border border-dashed border-sepia-solid/80", `o${i}`, o.label))}
      {state === "ready" && bbox && box(bbox, "absolute rounded-sm bg-gold-solid/15 outline outline-[3px] outline-offset-[3px] outline-gold-solid", "cited")}
      <figcaption className="sr-only">The cited value is boxed in gold; other extracted values are outlined.</figcaption>
    </figure>
  );
}

// ------------------------------------------------------------------ document evidence

type Fact = { field: string; label: string; value: number | null; unit: string; page: number | null; bbox: number[] | null; confidence: number | null; review_status: string; source_ref_id: number; conversions?: Conversion[] };
type Conversion = { field: string; printed: string; from: string; to: string; factor: number; value: number };

/** The printed value and what the canonical layer made of it (the MD when there are several). */
function conversionOf(f: Fact): Conversion | undefined {
  const cs = f.conversions ?? [];
  return cs.find((c) => /(^|\.)md$/.test(c.field)) ?? cs[0];
}
type Fields = { document_id: number; title: string; type: string; pages: number | null; synthetic: boolean; facts: Fact[]; review: { id: number; field: string; value: unknown; printed: string | null; page: number | null; bbox: number[] | null; confidence: number }[] };

function DocumentEvidence({ c }: { c: Citation }) {
  const [page, setPage] = useState(c.page ?? 1);
  const [pages, setPages] = useState<number | null>(null);
  const [focus, setFocus] = useState<{ bbox: number[] | null; key: string } | null>(c.bbox ? { bbox: c.bbox, key: "cited" } : null);
  const [fields, setFields] = useState<Fields | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    api<Fields>(`/documents/${c.document_id}/fields`).then(setFields, (e) => setErr(e instanceof ApiError ? e.detail : String(e)));
  }, [c.document_id]);
  const onPages = useCallback((n: number) => setPages(n), []);
  const here = (fields?.facts ?? []).filter((f) => f.page === page && f.bbox);
  const held = (fields?.review ?? []).filter((r) => r.page === page);
  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
      <div className="grid gap-2">
        <div className="flex items-center gap-2">
          <Button variant="secondary" size="icon" aria-label="Previous page" disabled={page <= 1} onClick={() => setPage(page - 1)}>
            <ChevronLeft />
          </Button>
          <span className="num text-sm" aria-live="polite">
            page {page}
            {pages ? ` of ${pages}` : ""}
          </span>
          <Button variant="secondary" size="icon" aria-label="Next page" disabled={pages != null && page >= pages} onClick={() => setPage(page + 1)}>
            <ChevronRight />
          </Button>
        </div>
        <PdfPage
          documentId={c.document_id!}
          page={page}
          bbox={page === (c.page ?? page) || focus?.key !== "cited" ? focus?.bbox : null}
          others={here.map((f) => ({ bbox: f.bbox!, label: `${words(f.field)} ${f.label} ${f.value ?? ""} ${f.unit}` }))}
          onPages={onPages}
          label={`${c.document}, page ${page}`}
        />
      </div>
      <aside aria-label="Extracted from this page" className="grid content-start gap-3">
        {err && <Notice tone="advisory">{err}</Notice>}
        {!fields && !err && <Skeleton className="h-40" />}
        {fields && (
          <>
            <h3 className="m-0 text-sm font-semibold">Extracted on page {page}</h3>
            {here.length === 0 && <p className="m-0 text-sm text-muted">No stored values on this page.</p>}
            <ul className="m-0 grid list-none gap-1 p-0">
              {here.map((f) => {
                const active = focus?.key === `f${f.source_ref_id}`;
                return (
                  <li key={f.source_ref_id}>
                    <button
                      type="button"
                      onClick={() => setFocus({ bbox: f.bbox, key: `f${f.source_ref_id}` })}
                      className={cn("grid w-full grid-cols-[1fr_auto] gap-x-2 rounded-md border px-3 py-2 text-left text-sm", active ? "border-gold-solid bg-gold-subtle" : "border-line hover:bg-surface-2")}
                    >
                      <span>
                        {words(f.field.replace("event:", "event: "))} {f.label && <span className="text-muted">· {f.label}</span>}
                      </span>
                      <span className="num font-semibold">
                        {f.value ?? "–"} {f.unit}
                      </span>
                      {conversionOf(f) && (
                        <span className="col-span-2 text-xs" data-testid="depth-conversion">
                          printed <span className="num">“{conversionOf(f)!.printed} {conversionOf(f)!.from}”</span> → stored{" "}
                          <span className="num font-semibold">{conversionOf(f)!.value.toLocaleString(undefined, { maximumFractionDigits: 1 })} {conversionOf(f)!.to}</span>
                          <span className="text-muted"> (× {conversionOf(f)!.factor})</span>
                        </span>
                      )}
                      <span className="col-span-2 flex items-center gap-2 text-xs text-muted">
                        <GradeBadge grade={gradeOf(f.review_status, f.confidence)} className="scale-75" />
                        confidence {pct(f.confidence)} · {f.review_status}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
            {held.length > 0 && (
              <>
                <h3 className="m-0 mt-2 text-sm font-semibold">Held for review on this page</h3>
                <ul className="m-0 grid list-none gap-1 p-0">
                  {held.map((r) => (
                    <li key={r.id} className="rounded-md border border-warning-solid/60 bg-warning-subtle px-3 py-2 text-sm">
                      {words(r.field)}: <span className="num">{String(r.value ?? "–")}</span>
                      {r.printed && <span className="text-muted"> (printed “{r.printed}”)</span>}
                      <span className="block text-xs text-muted">confidence {pct(r.confidence)}</span>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </>
        )}
      </aside>
    </div>
  );
}

// ------------------------------------------------------------------ voice-note evidence

type Note = { id: number; uri: string; duration_s: number; speaker: string | null; lang: string; well: string | null; formation: string | null; spans: { source_ref_id: number; t_start: number | null; t_end: number | null; text: string | null; review_status: string }[] };

export function AudioSpan({ noteId, start, end }: { noteId: number; start: number; end: number }) {
  const audio = useRef<HTMLAudioElement>(null);
  const [src, setSrc] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [playing, setPlaying] = useState(false);
  const [t, setT] = useState(start);
  const [note, setNote] = useState<Note | null>(null);
  useEffect(() => {
    let url: string | null = null;
    fileOf(`/audio-notes/${noteId}/file`).then(
      (b) => {
        url = URL.createObjectURL(new Blob([b]));
        setSrc(url);
      },
      (e) => setErr(e instanceof ApiError && e.status === 404 ? "The recording is not stored; the transcript span is below." : String(e)),
    );
    api<Note>(`/audio-notes/${noteId}`).then(setNote, () => undefined);
    return () => {
      if (url) URL.revokeObjectURL(url);
    };
  }, [noteId]);
  const playSpan = () => {
    const a = audio.current;
    if (!a) return;
    if (playing) {
      a.pause();
      return;
    }
    a.currentTime = start;
    void a.play();
  };
  const duration = note?.duration_s ?? Math.max(end, 1);
  return (
    <div className="grid gap-3" data-testid="audio-evidence">
      {err && <Notice tone="advisory">{err}</Notice>}
      {src && (
        <audio
          ref={audio}
          src={src}
          preload="auto"
          controls
          className="w-full"
          onLoadedMetadata={(e) => {
            e.currentTarget.currentTime = start;
          }}
          onTimeUpdate={(e) => {
            const a = e.currentTarget;
            setT(a.currentTime);
            if (playing && a.currentTime >= end) a.pause();
          }}
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          data-testid="audio"
        />
      )}
      <div className="flex items-center gap-3">
        <Button onClick={playSpan} disabled={!src} variant="secondary">
          {playing ? <Pause /> : <Play />} {playing ? "Pause" : `Play cited span ${start.toFixed(1)}–${end.toFixed(1)} s`}
        </Button>
        <span className="num text-sm text-muted" aria-live="off">
          {t.toFixed(1)} s
        </span>
      </div>
      <div className="relative h-3 rounded-full bg-surface-2" role="img" aria-label={`Cited span ${start} to ${end} seconds of ${duration.toFixed(0)} s`}>
        <div className="absolute inset-y-0 rounded-full bg-gold-solid" style={{ left: `${(start / duration) * 100}%`, width: `${((end - start) / duration) * 100}%` }} />
        <div className="absolute inset-y-[-3px] w-0.5 bg-cyan-solid" style={{ left: `${Math.min(100, (t / duration) * 100)}%` }} />
      </div>
      {note && (
        <dl className="m-0 grid grid-cols-[110px_1fr] gap-x-3 gap-y-1 text-sm">
          <dt className="text-muted">Speaker</dt>
          <dd className="m-0">{note.speaker ?? "–"}</dd>
          <dt className="text-muted">Well</dt>
          <dd className="m-0">{[note.well, note.formation].filter(Boolean).join(" · ") || "–"}</dd>
          <dt className="text-muted">Transcript</dt>
          <dd className="m-0">
            {note.spans.map((s) => (
              <p key={s.source_ref_id} className={cn("m-0 mb-1 rounded px-1", s.t_start === start && "bg-gold-subtle")}>
                <span className="num text-xs text-muted">{s.t_start?.toFixed(1)}–{s.t_end?.toFixed(1)} s </span>
                {s.text ?? "(no transcript text)"}
              </p>
            ))}
          </dd>
        </dl>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ the sheet

/** Resolve a citation that only names its source_ref (alerts, roadmap evidence). */
function useResolved(c: Citation | null): Citation | null {
  const [r, setR] = useState<Citation | null>(null);
  useEffect(() => {
    setR(null);
    if (!c) return;
    if (c.document_id || c.audio_note_id || !c.source_ref_id) {
      setR(c);
      return;
    }
    api<{ document_id: number | null; document: string | null; page: number | null; bbox: number[] | null; audio_note_id: number | null; t_start: number | null; t_end: number | null; review_status: string; synthetic: boolean }>(
      `/source-refs/${c.source_ref_id}`,
    ).then(
      (s) =>
        setR({
          ...c,
          type: s.audio_note_id ? "audio" : "document",
          document_id: s.document_id ?? undefined,
          document: s.document ?? c.document,
          page: s.page ?? c.page,
          bbox: s.bbox,
          audio_note_id: s.audio_note_id ?? undefined,
          t_start: s.t_start ?? undefined,
          t_end: s.t_end ?? undefined,
          review_status: s.review_status,
          synthetic: s.synthetic,
        }),
      () => setR(c),
    );
  }, [c]);
  return r;
}

/** The evidence viewer: the page with the value boxed and what was extracted beside it, or
 *  the recording at the cited time, or the query and rows. */
export function EvidenceSheet({ c, onOpenChange }: { c: Citation | null; onOpenChange: (o: boolean) => void }) {
  const r = useResolved(c);
  const grade = r ? gradeOf(r.review_status, null, r.type === "aggregate") : 4;
  return (
    <Sheet open={c != null} onOpenChange={onOpenChange} wide title={c ? `${c.id} · ${labelOf(c)}` : ""} description={r?.synthetic ? "Synthetic record" : undefined}>
      {!r ? (
        <Skeleton className="h-96" />
      ) : (
        <div className="grid gap-4" data-testid="evidence-viewer">
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <GradeBadge grade={grade} />
            {r.review_status && <span>Review: {r.review_status}</span>}
            {r.bbox && <span className="num text-muted">box {r.bbox.map((v) => v.toFixed(2)).join(", ")}</span>}
          </div>
          {r.type === "document" && r.document_id ? <DocumentEvidence c={r} /> : null}
          {r.type === "document" && !r.document_id && <Notice tone="advisory">This citation names {r.document} p. {r.page}, but the document is not stored.</Notice>}
          {r.type === "audio" && r.audio_note_id ? <AudioSpan noteId={r.audio_note_id} start={r.t_start ?? 0} end={r.t_end ?? (r.t_start ?? 0) + 5} /> : null}
          {r.type === "audio" && !r.audio_note_id && (
            <p>
              Voice note <span className="num">{r.uri}</span>, from <span className="num">{r.t_start} s</span> to <span className="num">{r.t_end} s</span>.
            </p>
          )}
          {r.type === "aggregate" && <p>Computed by rules from {r.derived_from?.join(", ")}: {r.text}</p>}
          {r.sql && <pre className="num overflow-auto rounded-md bg-surface-2 p-3 text-xs">{r.sql}</pre>}
        </div>
      )}
    </Sheet>
  );
}

/** Evidence chip that opens the viewer. */
export function Evidence({ c }: { c: Citation }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <EvidenceChip id={c.id} kind={kindOf(c)} label={labelOf(c)} onClick={() => setOpen(true)} />
      <EvidenceSheet c={open ? c : null} onOpenChange={setOpen} />
    </>
  );
}
