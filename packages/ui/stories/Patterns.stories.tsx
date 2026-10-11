import type { Meta, StoryObj } from "@storybook/react-vite";
import { Check, X } from "lucide-react";
import type { ReactNode } from "react";
import { EvidenceChip, FreshnessIndicator, Quantity, RiskTierBadge, RulesAIChip } from "../src/domain";
import { Card, CardBody } from "../src/primitives";

const meta: Meta = { title: "Patterns/Do and don't" };
export default meta;

function Pair({ title, good, bad, why }: { title: string; good: ReactNode; bad: ReactNode; why: string }) {
  return (
    <Card className="mb-4">
      <CardBody>
        <h3 className="m-0 mb-3 text-md">{title}</h3>
        <div className="grid gap-4 md:grid-cols-2">
          <div className="rounded-md border-2 border-teal p-3">
            <p className="m-0 mb-2 flex items-center gap-1 font-semibold text-teal">
              <Check aria-hidden className="size-4" /> Do
            </p>
            {good}
          </div>
          <div className="rounded-md border-2 border-critical p-3">
            <p className="m-0 mb-2 flex items-center gap-1 font-semibold text-critical">
              <X aria-hidden className="size-4" /> Don't
            </p>
            {bad}
          </div>
        </div>
        <p className="m-0 mt-3 text-sm text-muted">{why}</p>
      </CardBody>
    </Card>
  );
}

export const Rules: StoryObj = {
  render: () => (
    <div className="max-w-4xl">
      <Pair
        title="Risk is never colour alone"
        good={<RiskTierBadge tier="critical" />}
        bad={<span aria-hidden className="inline-block size-4 rounded-full bg-critical-solid" />}
        why="Icon, word and colour together: readable in sunlight, by colour-blind users and in greyscale screenshots."
      />
      <Pair
        title="Every number has its unit"
        good={<Quantity value={2153.1} unit="m MD" />}
        bad={<span>2153.1</span>}
        why="MD vs TVDSS, m vs ft, sg vs ppg: a bare number is an incident waiting to happen. Units in mono, muted."
      />
      <Pair
        title="Claims carry their evidence"
        good={
          <p className="m-0">
            Barail top at <Quantity value={2153.1} unit="m MD" /> <EvidenceChip id="E1" kind="document" label="SA-19 WCR p. 2" />
          </p>
        }
        bad={<p className="m-0">Barail top at about 2,150 m.</p>}
        why="Evidence chips open the source page with the value boxed; an uncited number is not shown."
      />
      <Pair
        title="Say who decided"
        good={
          <p className="m-0 flex items-center gap-2">
            Kick warning <RulesAIChip ai />
          </p>
        }
        bad={<p className="m-0">AI detected a kick</p>}
        why="Rules raise alerts; the AI only explains them. The R | AI chip says so wherever both are involved."
      />
      <Pair
        title="Stale data looks stale"
        good={<FreshnessIndicator state="stale" age="14 s" />}
        bad={<FreshnessIndicator state="live" />}
        why="When the feed stops, the screen turns grey within 10 s; old numbers must never look current."
      />
    </div>
  ),
};
