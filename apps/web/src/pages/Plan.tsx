import { Button, Card, CardBody, CardHeader, cn, FormationSwatch, Input, NativeSelect, Notice, RiskTierBadge, RulesAIChip, Skeleton } from "@repo/ui";
import { CircleCheck, CircleX, Download, FileDown, FileUp, Plus, ShieldCheck, Trash2 } from "lucide-react";
import { useState, type ChangeEvent } from "react";
import { api, API } from "../api";
import { can, token } from "../auth";
import { ErrorNote, NeedWell, PageHeader, SignInGate, useLoad } from "../components/common";
import { Evidence, type Citation } from "../components/EvidenceViewer";
import { useApp, useSelectedWell } from "../state";

type Section = { name: string; md_top: number; md_base: number; hole_in: number; casing_od_in: number | null; mud_type: string; mw_sg_min: number; mw_sg_max: number };
type Program = { td_md: number; ecd_margin_sg: number; sections: Section[]; cement: { casing_od_in: number; toc_md: number; slurry_density_sg: number }[] };
type Finding = { rule: string; severity: "block" | "warn" | "info"; message: string; section: string | null; formation: string | null; evidence: string[] };
type Ev = { id: string; kind: string; well: string; formation: string | null; md: number | null; value: number | null; unit: string | null; source?: { document?: string; page?: number; source_ref_id?: number } };
type Lint = { lint_run_id: number; findings: Finding[]; evidence: Ev[]; summary: Record<string, number> };
type Brief = { briefing_id: number; status: "verified" | "fallback"; briefing: { title: string; paragraphs: { text: string; citations: string[] }[] }; pdf_url: string };
type Formation = { formation: string; top_md: number | null; base_md?: number | null };

const SEV = { block: "critical", warn: "warning", info: "advisory" } as const;
const COLS: (keyof Section)[] = ["name", "md_top", "md_base", "hole_in", "casing_od_in", "mud_type", "mw_sg_min", "mw_sg_max"];
const TEMPLATE = `${COLS.join(",")}\n17-1/2 in surface,0,500,17.5,13.375,wbm,1.05,1.10\n12-1/4 in intermediate,500,2000,12.25,9.625,wbm,1.12,1.25\n8-1/2 in production,2000,3500,8.5,7,wbm,1.20,1.35\n`;

function defaults(td: number): Program {
  const a = Math.round(td * 0.15);
  const b = Math.round(td * 0.6);
  return {
    td_md: td,
    ecd_margin_sg: 0.05,
    sections: [
      { name: "17-1/2 in surface", md_top: 0, md_base: a, hole_in: 17.5, casing_od_in: 13.375, mud_type: "wbm", mw_sg_min: 1.05, mw_sg_max: 1.1 },
      { name: "12-1/4 in intermediate", md_top: a, md_base: b, hole_in: 12.25, casing_od_in: 9.625, mud_type: "wbm", mw_sg_min: 1.12, mw_sg_max: 1.25 },
      { name: "8-1/2 in production", md_top: b, md_base: td, hole_in: 8.5, casing_od_in: 7, mud_type: "wbm", mw_sg_min: 1.2, mw_sg_max: 1.35 },
    ],
    cement: [],
  };
}

/** A program from an uploaded file: JSON (the program, or {program}) or CSV of sections with
 *  the template's columns. Throws a message a user can act on. */
export function parseProgram(name: string, text: string, td: number): Program {
  if (name.toLowerCase().endsWith(".json")) {
    const j = JSON.parse(text);
    const p = (j.program ?? j) as Partial<Program>;
    if (!Array.isArray(p.sections) || !p.sections.length) throw new Error("The JSON has no `sections` list.");
    return { td_md: p.td_md ?? td, ecd_margin_sg: p.ecd_margin_sg ?? 0.05, sections: p.sections, cement: p.cement ?? [] };
  }
  const lines = text.trim().split(/\r?\n/).filter(Boolean);
  const head = lines[0].split(",").map((h) => h.trim());
  const missing = COLS.filter((c) => c !== "casing_od_in" && !head.includes(c));
  if (missing.length) throw new Error(`The CSV is missing column(s): ${missing.join(", ")}. Download the template.`);
  const sections = lines.slice(1).map((line, i) => {
    const cells = line.split(",").map((c) => c.trim());
    const row = Object.fromEntries(head.map((h, k) => [h, cells[k]]));
    const num = (k: string) => {
      const v = Number(row[k]);
      if (row[k] === "" || row[k] == null || !Number.isFinite(v)) throw new Error(`Row ${i + 2}: ${k} is not a number ("${row[k] ?? ""}").`);
      return v;
    };
    return {
      name: row.name || `section ${i + 1}`,
      md_top: num("md_top"),
      md_base: num("md_base"),
      hole_in: num("hole_in"),
      casing_od_in: row.casing_od_in ? num("casing_od_in") : null,
      mud_type: row.mud_type || "wbm",
      mw_sg_min: num("mw_sg_min"),
      mw_sg_max: num("mw_sg_max"),
    };
  });
  return { td_md: Math.max(td, ...sections.map((s) => s.md_base)), ecd_margin_sg: 0.05, sections, cement: [] };
}

function toCitation(e: Ev): Citation {
  return e.source?.document ? { id: e.id, type: "document", document: e.source.document, page: e.source.page, source_ref_id: e.source.source_ref_id } : { id: e.id, type: "query", view: "offset facts", kind: e.kind };
}

function ByFormation({ lint, forms }: { lint: Lint; forms: Formation[] }) {
  const ev = new Map(lint.evidence.map((e) => [e.id, e]));
  const rows = forms.filter((f) => f.top_md != null);
  const general = lint.findings.filter((f) => !f.formation || !rows.some((r) => r.formation === f.formation));
  return (
    <div className="grid gap-2" data-testid="lint-by-formation">
      <table className="w-full border-collapse text-sm">
        <caption className="sr-only">Findings by formation</caption>
        <thead>
          <tr className="text-left text-xs uppercase text-muted">
            <th scope="col" className="w-44 px-2 py-2">Formation</th>
            <th scope="col" className="w-28 px-2 py-2">Result</th>
            <th scope="col" className="px-2 py-2">Findings and the offsets they cite</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((f, i) => {
            const fs = lint.findings.filter((x) => x.formation === f.formation);
            const fail = fs.some((x) => x.severity !== "info");
            const checked = [...new Set(lint.evidence.filter((e) => e.formation === f.formation).map((e) => e.well))];
            return (
              <tr key={f.formation} className="border-t border-line align-top" data-testid={`lint-row-${f.formation}`}>
                <th scope="row" className="px-2 py-3 text-left font-normal">
                  <FormationSwatch name={f.formation} index={i} />
                  <div className="num mt-1 text-xs text-muted">{Math.round(f.top_md!).toLocaleString()} m MD</div>
                </th>
                <td className="px-2 py-3">
                  {fail ? (
                    <span className="inline-flex items-center gap-1.5 font-semibold text-critical">
                      <CircleX aria-hidden className="size-5" /> Fail
                    </span>
                  ) : (
                    <span className="inline-flex items-center gap-1.5 font-semibold text-teal">
                      <CircleCheck aria-hidden className="size-5" /> Pass
                    </span>
                  )}
                </td>
                <td className="px-2 py-3">
                  {fs.length === 0 && (
                    <span className="text-muted">
                      No finding. {checked.length ? `Checked against ${checked.length} offset${checked.length > 1 ? "s" : ""}: ${checked.join(", ")}.` : "No offset evidence in this formation."}
                    </span>
                  )}
                  <ul className="m-0 grid list-none gap-2 p-0">
                    {fs.map((x, k) => (
                      <li key={k}>
                        <div className="flex flex-wrap items-center gap-2">
                          <RiskTierBadge tier={SEV[x.severity]} />
                          <span className="num text-xs text-muted">{x.rule}</span>
                          {x.section && <span className="text-xs">{x.section}</span>}
                        </div>
                        <p className="mb-1 mt-1">{x.message}</p>
                        <div className="flex flex-wrap gap-1.5">
                          {x.evidence.map((id) => {
                            const e = ev.get(id);
                            return e ? (
                              <span key={id} className="inline-flex items-center gap-1">
                                <span className="num text-xs text-muted">{e.well}</span>
                                <Evidence c={toCitation(e)} />
                              </span>
                            ) : null;
                          })}
                        </div>
                      </li>
                    ))}
                  </ul>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {general.length > 0 && (
        <div className="rounded-md border border-line p-3">
          <h3 className="m-0 text-sm font-semibold">Program-wide</h3>
          {general.map((x, k) => (
            <p key={k} className="mb-0 mt-2 flex flex-wrap items-center gap-2">
              <RiskTierBadge tier={SEV[x.severity]} /> {x.message}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}

function PlanBody({ id }: { id: number }) {
  const { user } = useApp();
  const w = useSelectedWell();
  const forms = useLoad<Formation[]>(`/wellbores/${id}/formations`);
  const td0 = w?.td_md ? Math.round(w.td_md) : 3000;
  const [prog, setProg] = useState<Program>(() => defaults(td0));
  const [source, setSource] = useState<string>("default sections from the well's TD");
  const [lint, setLint] = useState<Lint | null>(null);
  const [brief, setBrief] = useState<Brief | null>(null);
  const [busy, setBusy] = useState<"lint" | "brief" | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const write = can(user, "plan");

  const set = (i: number, k: keyof Section, v: string) =>
    setProg((p) => ({ ...p, sections: p.sections.map((s, j) => (j === i ? { ...s, [k]: k === "name" || k === "mud_type" ? v : v === "" ? null : Number(v) } : s)) }));

  async function upload(e: ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    e.target.value = "";
    if (!f) return;
    setErr(null);
    try {
      setProg(parseProgram(f.name, await f.text(), td0));
      setSource(`uploaded ${f.name}`);
      setLint(null);
      setBrief(null);
    } catch (x) {
      setErr(x);
    }
  }

  async function check() {
    setBusy("lint");
    setErr(null);
    setBrief(null);
    try {
      setLint(await api<Lint>("/programs/lint", { json: { program: { wellbore_id: id, ...prog } } }));
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(null);
    }
  }

  async function writeBriefing() {
    if (!lint) return;
    setBusy("brief");
    try {
      setBrief(await api<Brief>(`/briefings/${id}`, { json: { lint_run_id: lint.lint_run_id } }));
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(null);
    }
  }

  async function pdf() {
    if (!brief) return;
    const r = await fetch(`${API}${brief.pdf_url}`, { headers: { Authorization: `Bearer ${token() ?? ""}` } });
    if (!r.ok) return setErr(new Error(`PDF not available (${r.status})`));
    const a = document.createElement("a");
    a.href = URL.createObjectURL(await r.blob());
    a.download = `${w?.wellbore ?? "well"}-briefing-${brief.briefing_id}.pdf`;
    a.click();
  }

  const ev = new Map((lint?.evidence ?? []).map((e) => [e.id, e]));
  const cell = "w-full min-w-[84px]";
  const failed = lint ? new Set(lint.findings.filter((f) => f.severity !== "info" && f.formation).map((f) => f.formation)).size : 0;
  return (
    <>
      <PageHeader
        title={`Program linter · ${w?.wellbore ?? ""}`}
        description="Upload or edit a drilling program; it is checked formation by formation against the hazard roadmap and the offsets' losses, kicks, stuck pipe and LOT/FIT. Then a briefing whose every number is checked against the cited records."
      />
      {!write && <Notice tone="advisory">Checking a program needs the planner role.</Notice>}
      <Card className="mb-4">
        <CardHeader
          title="Program"
          description={<>Metres MD, sg, inches · {source}</>}
          actions={
            <>
              <a className="inline-flex min-h-target items-center gap-2 rounded-md px-3 text-sm hover:bg-surface-2" href={`data:text/csv;charset=utf-8,${encodeURIComponent(TEMPLATE)}`} download="program-template.csv">
                <Download aria-hidden className="size-4" /> CSV template
              </a>
              <label className="inline-flex min-h-target cursor-pointer items-center gap-2 rounded-md border border-line-strong px-3 text-sm font-medium hover:bg-surface-2 focus-within:outline focus-within:outline-[3px] focus-within:outline-[var(--focus)]">
                <FileUp aria-hidden className="size-4" /> Upload program
                <input type="file" accept=".json,.csv,application/json,text/csv" className="sr-only" onChange={upload} data-testid="program-upload" />
              </label>
            </>
          }
        />
        <CardBody className="grid gap-4">
          <div className="flex flex-wrap gap-4">
            <label className="grid gap-1 text-sm">
              TD (m MD)
              <Input type="number" value={prog.td_md} onChange={(e) => setProg({ ...prog, td_md: Number(e.target.value) })} className="w-36" />
            </label>
            <label className="grid gap-1 text-sm">
              ECD margin (sg)
              <Input type="number" step="0.01" value={prog.ecd_margin_sg} onChange={(e) => setProg({ ...prog, ecd_margin_sg: Number(e.target.value) })} className="w-36" />
            </label>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <caption className="sr-only">Hole sections</caption>
              <thead>
                <tr className="text-left text-xs uppercase text-muted">
                  {["Section", "Top (m MD)", "Base (m MD)", "Hole (in)", "Casing OD (in)", "Mud", "MW min (sg)", "MW max (sg)", ""].map((h) => (
                    <th key={h} scope="col" className="px-1 py-1">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {prog.sections.map((s, i) => (
                  <tr key={i}>
                    <td className="px-1 py-1"><Input aria-label={`Section ${i + 1} name`} value={s.name} onChange={(e) => set(i, "name", e.target.value)} className="min-w-[180px]" /></td>
                    {(["md_top", "md_base", "hole_in", "casing_od_in"] as const).map((k) => (
                      <td key={k} className="px-1 py-1"><Input aria-label={`Section ${i + 1} ${k}`} type="number" step="any" value={s[k] ?? ""} onChange={(e) => set(i, k, e.target.value)} className={cell} /></td>
                    ))}
                    <td className="px-1 py-1">
                      <NativeSelect aria-label={`Section ${i + 1} mud type`} value={s.mud_type} onChange={(e) => set(i, "mud_type", e.target.value)} className="min-w-[96px]">
                        {["wbm", "obm", "sbm", "other"].map((m) => <option key={m}>{m}</option>)}
                      </NativeSelect>
                    </td>
                    {(["mw_sg_min", "mw_sg_max"] as const).map((k) => (
                      <td key={k} className="px-1 py-1"><Input aria-label={`Section ${i + 1} ${k}`} type="number" step="0.01" value={s[k]} onChange={(e) => set(i, k, e.target.value)} className={cell} /></td>
                    ))}
                    <td className="px-1 py-1">
                      <Button variant="ghost" size="icon" aria-label={`Remove ${s.name}`} onClick={() => setProg((p) => ({ ...p, sections: p.sections.filter((_, j) => j !== i) }))}>
                        <Trash2 />
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button variant="secondary" onClick={() => setProg((p) => ({ ...p, sections: [...p.sections, { ...p.sections[p.sections.length - 1], name: "new section" }] }))}>
              <Plus /> Add section
            </Button>
            <Button onClick={check} disabled={busy != null || !write || prog.sections.length === 0}>
              <ShieldCheck /> {busy === "lint" ? "Checking…" : "Check program"}
            </Button>
          </div>
        </CardBody>
      </Card>
      <ErrorNote error={err} />
      {busy === "lint" && <Skeleton className="mb-4 h-64 w-full" />}
      {lint && (
        <Card className="mb-4">
          <CardHeader
            title={failed ? `${failed} formation${failed > 1 ? "s" : ""} fail` : "No blocking finding"}
            description={`Ruleset lint@1.0.0 · ${Object.entries(lint.summary).map(([k, v]) => `${v} ${k}`).join(" · ")} · lint run ${lint.lint_run_id}`}
            actions={
              <>
                <RulesAIChip ai={false} className="shrink-0" />
                <Button variant="ai" onClick={writeBriefing} disabled={busy != null}>
                  {busy === "brief" ? "Writing…" : "Write briefing"}
                </Button>
              </>
            }
          />
          <CardBody>
            {forms.data ? <ByFormation lint={lint} forms={forms.data} /> : <Skeleton className="h-48" />}
          </CardBody>
        </Card>
      )}
      {brief && (
        <Card data-testid="briefing">
          <CardHeader
            title={brief.briefing.title}
            description={brief.status === "verified" ? "Written by the model; every number checked against the cited evidence." : "No model draft passed verification: this is the rule-written text."}
            actions={
              <>
                <RulesAIChip ai={brief.status === "verified"} className="shrink-0" />
                <Button variant="secondary" onClick={pdf}>
                  <FileDown /> Download PDF
                </Button>
              </>
            }
          />
          <CardBody className="grid gap-3">
            {brief.briefing.paragraphs.map((p, i) => (
              <div key={i} className={cn("rounded-md border-l-4 pl-3", p.text.startsWith("BLOCK") ? "border-critical-solid" : p.text.startsWith("WARN") ? "border-warning-solid" : "border-line")}>
                <p className="m-0">{p.text}</p>
                <div className="mt-1 flex flex-wrap gap-2">
                  {p.citations.map((c) => {
                    const e = ev.get(c);
                    return e ? <Evidence key={c} c={toCitation(e)} /> : null;
                  })}
                </div>
              </div>
            ))}
          </CardBody>
        </Card>
      )}
    </>
  );
}

export function PlanPage({ id }: { id: number | null }) {
  const { selected } = useApp();
  return (
    <SignInGate>
      <NeedWell id={id ?? selected}>{(i) => <PlanBody key={i} id={i} />}</NeedWell>
    </SignInGate>
  );
}
