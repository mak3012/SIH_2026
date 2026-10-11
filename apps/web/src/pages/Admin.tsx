import { Card, CardBody, CardHeader, DataTable, FreshnessIndicator, Notice, Skeleton, Stat } from "@repo/ui";
import { ErrorNote, PageHeader, SignInGate, useLoad } from "../components/common";
import { useApp } from "../state";

type Status = {
  user: { name: string; roles: string[]; blocks: string[]; jvs: string[] };
  counts: Record<string, number>;
  feeds: { wellbore_id: number; well: string; connector: string; state: string; last_sample_ts: string | null; updated_at: string }[];
  detectors: { path: string; present: boolean; sha256: string | null };
  llm: { backend: string; model: string | null; embeddings: string };
};

function AdminBody() {
  const { data, error, loading } = useLoad<Status>("/admin/status");
  if (loading) return <Skeleton className="h-64 w-full" />;
  if (error) return <ErrorNote error={error} />;
  if (!data) return null;
  return (
    <div className="grid gap-4">
      <Card>
        <CardHeader title="Records" description={`Signed in as ${data.user.name} (${data.user.roles.join(", ")}); blocks ${data.user.blocks.join(", ") || "none"}.`} />
        <CardBody className="flex flex-wrap gap-6">
          {Object.entries(data.counts).map(([k, v]) => (
            <Stat key={k} label={k.replaceAll("_", " ")} accent="petrol">
              <span className="num">{v.toLocaleString()}</span>
            </Stat>
          ))}
        </CardBody>
      </Card>
      <Card>
        <CardHeader title="Models" />
        <CardBody className="grid gap-3">
          {data.detectors.present ? (
            <p className="m-0">
              Detector bank <span className="num">{data.detectors.path}</span>, sha256 <span className="num">{data.detectors.sha256 ?? "not recorded"}</span>.
            </p>
          ) : (
            <Notice tone="warning" title="No detector bank">
              Alerts run on physics rules only. Train with <span className="num">make detectors</span>.
            </Notice>
          )}
          <p className="m-0">
            LLM: <span className="num">{data.llm.backend}</span>
            {data.llm.model && <span className="num"> · {data.llm.model}</span>}; embeddings <span className="num">{data.llm.embeddings}</span>.
            {data.llm.backend === "none" && " Answers and briefings use the rule composer."}
          </p>
        </CardBody>
      </Card>
      <Card>
        <CardHeader title="Real-time feeds" />
        <CardBody className="overflow-auto p-0">
          <DataTable
            caption="Real-time feeds"
            head={["Well", "Connector", "State", "Last sample", "Updated"]}
            rows={data.feeds.map((f) => [
              <span key="w" className="num">{f.well}</span>,
              f.connector,
              <FreshnessIndicator key="s" state={f.state as "live"} />,
              <span key="l" className="num">{f.last_sample_ts ? new Date(f.last_sample_ts).toLocaleString() : "–"}</span>,
              <span key="u" className="num">{new Date(f.updated_at).toLocaleString()}</span>,
            ])}
          />
        </CardBody>
      </Card>
    </div>
  );
}

export function AdminPage() {
  const { user } = useApp();
  return (
    <>
      <PageHeader title="Admin" description="System status: record counts, models and feeds." />
      <SignInGate>{user?.roles.includes("admin") ? <AdminBody /> : <Notice tone="advisory">This page needs the admin role.</Notice>}</SignInGate>
    </>
  );
}
