import { Button, Card, CardBody, CardHeader, cn, Field, Input, NativeSelect, Notice, Skeleton } from "@repo/ui";
import { CloudOff, Mic, RotateCw, Save, Square, Trash2, UploadCloud } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { can } from "../auth";
import { DataState, PageHeader, SignInGate, useLoad } from "../components/common";
import { useApp } from "../state";
import { enqueue, flush, queued, remove, type Queued } from "../voiceQueue";

type ServerNote = { id: number; well: string | null; formation: string | null; note: string | null; duration_s: number; recorded_at: string | null; status: "transcribing" | "pending_confirmation" | "confirmed" | "reviewed"; lessons: number; open_review_items: number };

const STATUS: Record<ServerNote["status"], [string, string]> = {
  transcribing: ["Uploaded · transcribing", "border-line-strong text-muted"],
  pending_confirmation: ["Pending expert confirmation", "border-gold-solid bg-gold-subtle text-gold"],
  confirmed: ["Confirmed by an expert", "border-teal-solid bg-teal-subtle text-teal"],
  reviewed: ["Reviewed, nothing kept", "border-line-strong text-muted"],
};

function useOnline(): boolean {
  const [on, setOn] = useState(navigator.onLine);
  useEffect(() => {
    const up = () => setOn(true);
    const down = () => setOn(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
    };
  }, []);
  return on;
}

const mmss = (s: number) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

function Recorder({ onSaved }: { onSaved: () => void }) {
  const { wells, selected } = useApp();
  const [well, setWell] = useState<number | null>(selected);
  const forms = useLoad<{ formation: string; top_md: number | null }[]>(well ? `/wellbores/${well}/formations` : null);
  const [formation, setFormation] = useState("");
  const [note, setNote] = useState("");
  const [rec, setRec] = useState<MediaRecorder | null>(null);
  const [clip, setClip] = useState<{ blob: Blob; url: string; seconds: number } | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [level, setLevel] = useState(0);
  const [err, setErr] = useState<string | null>(null);
  const started = useRef(0);
  const chunks = useRef<Blob[]>([]);

  async function start() {
    setErr(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
      const r = new MediaRecorder(stream);
      const ctx = new AudioContext();
      const an = ctx.createAnalyser();
      an.fftSize = 512;
      ctx.createMediaStreamSource(stream).connect(an);
      const buf = new Float32Array(an.fftSize);
      const timer = setInterval(() => {
        an.getFloatTimeDomainData(buf);
        setLevel(Math.min(1, Math.sqrt(buf.reduce((s, v) => s + v * v, 0) / buf.length) * 4));
        setElapsed((Date.now() - started.current) / 1000);
      }, 100);
      chunks.current = [];
      r.ondataavailable = (e) => chunks.current.push(e.data);
      r.onstop = () => {
        clearInterval(timer);
        void ctx.close();
        stream.getTracks().forEach((t) => t.stop());
        const blob = new Blob(chunks.current, { type: r.mimeType });
        setClip({ blob, url: URL.createObjectURL(blob), seconds: (Date.now() - started.current) / 1000 });
        setRec(null);
        setLevel(0);
      };
      started.current = Date.now();
      setElapsed(0);
      r.start(1000);
      setRec(r);
    } catch (e) {
      setErr(e instanceof Error && e.name === "NotAllowedError" ? "Microphone access was refused. Allow it in the browser to record." : String(e));
    }
  }

  async function save() {
    if (!clip || !well) return;
    const w = wells?.find((x) => x.wellbore_id === well);
    await enqueue({
      client_id: crypto.randomUUID().replace(/-/g, ""),
      wellbore_id: well,
      well: w?.wellbore ?? String(well),
      formation: formation || null,
      note: note.trim(),
      recorded_at: new Date().toISOString(),
      duration_s: Math.max(0.1, clip.seconds),
      blob: clip.blob,
    });
    URL.revokeObjectURL(clip.url);
    setClip(null);
    setNote("");
    onSaved();
  }

  return (
    <Card>
      <CardHeader title="Record a note" description="Tag the well and formation; it is kept on this device until it uploads." />
      <CardBody className="grid gap-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Well">
            <NativeSelect value={well ?? ""} onChange={(e) => setWell(e.target.value ? Number(e.target.value) : null)} data-testid="voice-well">
              <option value="">Select…</option>
              {(wells ?? []).map((w) => (
                <option key={w.wellbore_id} value={w.wellbore_id}>
                  {w.wellbore}
                </option>
              ))}
            </NativeSelect>
          </Field>
          <Field label="Formation (optional)">
            <NativeSelect value={formation} onChange={(e) => setFormation(e.target.value)} disabled={!forms.data} data-testid="voice-formation">
              <option value="">Not sure</option>
              {(forms.data ?? []).map((f) => (
                <option key={f.formation}>{f.formation}</option>
              ))}
            </NativeSelect>
          </Field>
        </div>
        <Field label="Short note (optional)">
          <Input value={note} onChange={(e) => setNote(e.target.value)} maxLength={500} placeholder="e.g. tight hole at the coal, 1,850 m" />
        </Field>
        <div className="flex flex-wrap items-center gap-4">
          {rec ? (
            <Button variant="danger" onClick={() => rec.stop()} className="min-h-[64px] px-6 text-lg" data-testid="voice-stop">
              <Square /> Stop
            </Button>
          ) : (
            <Button onClick={start} disabled={!well || !!clip} className="min-h-[64px] px-6 text-lg" data-testid="voice-record">
              <Mic /> Record
            </Button>
          )}
          <span className="num text-2xl font-semibold" aria-live="off">
            {mmss(rec ? elapsed : (clip?.seconds ?? 0))}
          </span>
          <span className="h-3 w-40 rounded-full bg-surface-2" role="meter" aria-label="Input level" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(level * 100)}>
            <span className={cn("block h-3 rounded-full", level > 0.9 ? "bg-warning-solid" : "bg-teal-solid")} style={{ width: `${level * 100}%` }} />
          </span>
        </div>
        {err && <Notice tone="warning">{err}</Notice>}
        {clip && (
          <div className="flex flex-wrap items-center gap-3 rounded-md border border-line p-3">
            <audio src={clip.url} controls className="min-w-[240px] flex-1" />
            <Button onClick={save} data-testid="voice-save">
              <Save /> Save
            </Button>
            <Button variant="ghost" onClick={() => (URL.revokeObjectURL(clip.url), setClip(null))}>
              <Trash2 /> Discard
            </Button>
          </div>
        )}
      </CardBody>
    </Card>
  );
}

function VoiceBody() {
  const { user } = useApp();
  const online = useOnline();
  const [local, setLocal] = useState<Queued[] | null>(null);
  const [busy, setBusy] = useState(false);
  const server = useLoad<ServerNote[]>("/voice-notes");
  const refresh = useCallback(() => {
    queued().then(setLocal, () => setLocal([]));
  }, []);
  const send = useCallback(async () => {
    setBusy(true);
    try {
      if ((await flush()) > 0) server.reload();
    } finally {
      setBusy(false);
      refresh();
    }
  }, [refresh, server]);
  useEffect(refresh, [refresh]);
  useEffect(() => {
    if (online) void send();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [online]);

  if (!can(user, "operate")) return <Notice tone="advisory">Recording notes needs the wellsite engineer or superintendent role.</Notice>;
  return (
    <div className="grid gap-4 xl:grid-cols-2">
      <Recorder
        onSaved={() => {
          refresh();
          void send();
        }}
      />
      <div className="grid content-start gap-4">
        <Card>
          <CardHeader
            title="On this device"
            description={online ? "Uploads as soon as it is saved." : "Offline: notes wait here and upload when the network is back."}
            actions={
              <Button variant="secondary" size="sm" onClick={send} disabled={!online || busy || !local?.length}>
                {online ? <UploadCloud /> : <CloudOff />} {busy ? "Uploading…" : "Upload now"}
              </Button>
            }
          />
          <CardBody className="grid gap-2" data-testid="voice-queue">
            {local == null && <Skeleton className="h-16" />}
            {local?.length === 0 && <p className="m-0 text-muted">Nothing waiting.</p>}
            {local?.map((q) => (
              <div key={q.client_id} className="flex flex-wrap items-center gap-3 rounded-md border border-line px-3 py-2">
                <span className="num font-semibold">{q.well}</span>
                <span className="text-sm">{q.formation ?? "formation not set"}</span>
                <span className="num text-sm text-muted">{mmss(q.duration_s)}</span>
                <span className="ml-auto rounded-full border border-line-strong px-2 text-xs">{online ? (q.attempts ? `failed ${q.attempts}×: ${q.last_error}` : "uploading") : "waiting for network"}</span>
                <Button variant="ghost" size="icon" aria-label="Delete this recording" onClick={() => remove(q.client_id).then(refresh)}>
                  <Trash2 />
                </Button>
              </div>
            ))}
          </CardBody>
        </Card>
        <Card>
          <CardHeader title="Uploaded" description="Transcribed, then lessons are drafted; nothing becomes a fact until an expert confirms it." actions={<Button variant="ghost" size="icon" aria-label="Refresh" onClick={server.reload}><RotateCw /></Button>} />
          <CardBody>
            <DataState state={server} skeleton="h-32" isEmpty={(d) => d.length === 0} empty={{ title: "No uploaded notes yet", body: "Recorded notes appear here once uploaded.", icon: <Mic /> }}>
              {(d) => (
                <ul className="m-0 grid list-none gap-2 p-0" data-testid="voice-uploaded">
                  {d.map((n) => {
                    const [label, cls] = STATUS[n.status];
                    return (
                      <li key={n.id} className="flex flex-wrap items-center gap-3 rounded-md border border-line px-3 py-2">
                        <span className="num font-semibold">{n.well ?? "–"}</span>
                        <span className="text-sm">{n.formation ?? "formation not set"}</span>
                        {n.note && <span className="text-sm text-muted">“{n.note}”</span>}
                        <span className={cn("ml-auto rounded-full border px-2.5 py-0.5 text-xs font-semibold", cls)}>{label}</span>
                      </li>
                    );
                  })}
                </ul>
              )}
            </DataState>
          </CardBody>
        </Card>
      </div>
    </div>
  );
}

export function VoicePage() {
  return (
    <>
      <PageHeader title="Voice note" description="Record what happened on the rig, tagged with the well and formation. Works offline; the recording uploads when there is signal." />
      <SignInGate>
        <VoiceBody />
      </SignInGate>
    </>
  );
}

