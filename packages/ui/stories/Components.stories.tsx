import type { Meta, StoryObj } from "@storybook/react-vite";
import { Bot, Send } from "lucide-react";
import { useState } from "react";
import {
  EvidenceChip,
  FormationSwatch,
  FreshnessIndicator,
  GradeBadge,
  Quantity,
  RigStatePill,
  RiskTierBadge,
  RulesAIChip,
  SeverityBadge,
  Stat,
  ThemeToggle,
} from "../src/domain";
import {
  Button,
  Card,
  CardBody,
  CardHeader,
  DataTable,
  EmptyState,
  Field,
  Input,
  NativeSelect,
  Notice,
  Sheet,
  Skeleton,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  Tip,
} from "../src/primitives";

const meta: Meta = { title: "Components" };
export default meta;

export const Buttons: StoryObj = {
  render: () => (
    <div className="flex flex-wrap gap-3">
      <Button>Acknowledge</Button>
      <Button variant="secondary">Shelve…</Button>
      <Button variant="ghost">Cancel</Button>
      <Button variant="danger">Stop replay</Button>
      <Button variant="ai">
        <Bot /> Ask
      </Button>
      <Button size="icon" variant="secondary" aria-label="Send">
        <Send />
      </Button>
      <Button disabled>Disabled</Button>
    </div>
  ),
};

export const RiskTiers: StoryObj = {
  render: () => (
    <div className="flex flex-wrap items-center gap-3">
      <RiskTierBadge tier="advisory" />
      <RiskTierBadge tier="warning" />
      <RiskTierBadge tier="critical" />
      <RiskTierBadge tier="warning">
        <span className="num font-normal">· kick 62 %</span>
      </RiskTierBadge>
    </div>
  ),
};

export const SeverityAndGrades: StoryObj = {
  name: "Severity S1–S6 and evidence grades G1–G4",
  render: () => (
    <div className="grid gap-4">
      <div className="flex gap-3">
        {([1, 2, 3, 4, 5, 6] as const).map((s) => (
          <SeverityBadge key={s} level={s} />
        ))}
      </div>
      <div className="flex gap-3">
        {([1, 2, 3, 4] as const).map((g) => (
          <GradeBadge key={g} grade={g} />
        ))}
      </div>
    </div>
  ),
};

export const RulesAndAI: StoryObj = {
  name: "R | AI chip",
  render: () => (
    <div className="flex gap-3">
      <RulesAIChip ai />
      <RulesAIChip ai={false} />
    </div>
  ),
};

export const Evidence: StoryObj = {
  render: () => (
    <div className="flex flex-wrap gap-2">
      <EvidenceChip id="E1" kind="document" label="SA-19 WCR p. 2" />
      <EvidenceChip id="E2" kind="audio" label="voice note 4.0–8.0 s" />
      <EvidenceChip id="E3" kind="query" label="chat.v_events" />
      <EvidenceChip id="E4" kind="aggregate" label="from E1–E3" />
      <EvidenceChip id="E5" kind="live" label="SPP 14:02:30" />
    </div>
  ),
};

export const LiveStatus: StoryObj = {
  render: () => (
    <div className="flex flex-wrap gap-3">
      <FreshnessIndicator state="live" age="2 s" />
      <FreshnessIndicator state="stale" age="14 s" />
      <FreshnessIndicator state="disconnected" />
      <FreshnessIndicator state="none" />
      <RigStatePill state="drilling_rotary" />
      <RigStatePill state="tripping_out" />
      <RigStatePill state="connection" />
    </div>
  ),
};

export const NumbersWithUnits: StoryObj = {
  render: () => (
    <div className="flex flex-wrap gap-8">
      <Stat label="Bit depth">
        <Quantity value={2153.12} unit="m MD" />
      </Stat>
      <Stat label="ECD" accent="cyan">
        <Quantity value={1.312} unit="sg" digits={2} />
      </Stat>
      <Stat label="Loss probability, Barail" accent="petrol" note="90 % interval 21–63 %, n_eff 4.2">
        <Quantity value={41} unit="%" digits={0} />
      </Stat>
      <Stat label="Missing value">
        <Quantity value={null} unit="bbl" />
      </Stat>
    </div>
  ),
};

export const CardsAndNotices: StoryObj = {
  render: () => (
    <div className="grid max-w-3xl gap-4">
      <Card>
        <CardHeader title="Hazard roadmap" description="Leave-one-out offsets: SA-05, SA-18" actions={<RulesAIChip ai={false} />} />
        <CardBody>
          <DataTable
            caption="Roadmap"
            head={["Formation", "Loss", "Kick"]}
            rows={[
              [<FormationSwatch key="b" name="Barail" index={2} />, "41 %", "4 %"],
              [<FormationSwatch key="k" name="Kopili" index={3} />, "12 %", "18 %"],
            ]}
          />
        </CardBody>
      </Card>
      <Notice tone="gold" title="Showstopper evidence">
        Two offsets recorded total losses within 30 m of the Barail top.
      </Notice>
      <Notice tone="warning">Feed stale for 14 s: numbers are not current.</Notice>
      <Card>
        <EmptyState title="No alerts">Rules raise alerts; AI only explains them.</EmptyState>
      </Card>
      <Skeleton className="h-6 w-64" />
    </div>
  ),
};

export const FormsAndTabs: StoryObj = {
  render: () => (
    <div className="grid max-w-xl gap-4">
      <Field label="Shelve reason" hint="Required. At most one shift.">
        <Input placeholder="Pit transfer in progress" />
      </Field>
      <Field label="Hazard">
        <NativeSelect>
          <option>Kick</option>
          <option>Loss</option>
        </NativeSelect>
      </Field>
      <Tabs defaultValue="roadmap">
        <TabsList aria-label="Well views">
          <TabsTrigger value="roadmap">Roadmap</TabsTrigger>
          <TabsTrigger value="3d">3D</TabsTrigger>
          <TabsTrigger value="alerts">Alerts</TabsTrigger>
        </TabsList>
        <TabsContent value="roadmap" className="py-3">Roadmap content</TabsContent>
        <TabsContent value="3d" className="py-3">3D content</TabsContent>
        <TabsContent value="alerts" className="py-3">Alerts content</TabsContent>
      </Tabs>
      <Tip label="Posterior = prior × likelihood ratio">
        <Button variant="secondary">Hover for a tooltip</Button>
      </Tip>
    </div>
  ),
};

function SheetDemo() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button onClick={() => setOpen(true)}>Open source page</Button>
      <Sheet open={open} onOpenChange={setOpen} title="SA-19 WCR (SYNTHETIC), page 2" description="Evidence E1 · G1 human-verified">
        <p>The page image with the cited box highlighted appears here.</p>
      </Sheet>
    </>
  );
}

export const SidePanel: StoryObj = { render: () => <SheetDemo /> };

export const Theme: StoryObj = { render: () => <ThemeToggle /> };
