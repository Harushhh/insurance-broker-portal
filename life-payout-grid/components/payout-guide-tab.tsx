import { Card, CardContent } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { CircleDollarSignIcon, RocketIcon, LayersIcon, RotateCcwIcon } from "lucide-react";

const GLOSSARY_CORE = [
  { term: "PPT", full: "Premium Paying Term", desc: "Number of years the customer pays premium." },
  { term: "PT", full: "Policy Term", desc: "Total duration of the policy / cover." },
  { term: "Base / Base Payout", full: "", desc: "The starting payout from the matching grid row (Insurer → Product → PPT → PT)." },
  { term: "Booster", full: "", desc: "Extra payout for eligible plans, PPTs or conditions, on top of the base." },
  { term: "Layering", full: "", desc: "Extra payout linked to monthly business / performance. Not fixed — varies month to month." },
  { term: "Renewal", full: "", desc: "Later-year (2nd year onward) payout. Kept separate from the first-year payout." },
];

const GLOSSARY_MORE = [
  { term: "Single Pay (SP)", desc: "Premium is paid once, upfront." },
  { term: "Regular Pay (RP)", desc: "Premium is paid regularly for the full policy term." },
  { term: "Limited Pay", desc: "Premium is paid for fewer years than the policy term." },
  { term: "Term", desc: "Protection-focused life insurance, typically no maturity payout." },
  { term: "Investment / Traditional", desc: "Savings, income or guaranteed-benefit oriented life plans." },
  { term: "ULIP / TULIP", desc: "Life cover bundled with market-linked investment funds." },
  { term: "PAR", desc: "Participating plan — may receive insurer-declared bonuses." },
  { term: "Non-Par", desc: "Non-participating plan — does not receive declared bonuses." },
  { term: "Pension / Annuity", desc: "Retirement-focused plans, often designed to create income." },
  { term: "ROP / NROP", desc: "Return of Premium / No Return of Premium." },
  { term: "POS", desc: "Point of Sale product category." },
  { term: "Persistency", desc: "How well policies continue and premiums keep getting paid — affects clawback." },
];

const EXAMPLE_ROWS = [
  { ppt: "5–9", pt: "—", type: "Term", base: "32%", renewal: "3.75%" },
  { ppt: "10–11", pt: "—", type: "Term", base: "34%", renewal: "3.98%" },
];

export function PayoutGuideTab() {
  return (
    <div className="space-y-10">
      <section className="space-y-4">
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Life payout in 2 minutes
          </p>
          <h2 className="text-lg font-semibold">
            Overall Payout = Base Payout + Booster + Layering
          </h2>
        </div>
        <div className="grid gap-3 sm:grid-cols-3">
          <GuideStat
            icon={<CircleDollarSignIcon className="size-4" />}
            title="Base Payout"
            subtitle="From the exact grid row"
          />
          <GuideStat
            icon={<RocketIcon className="size-4" />}
            title="Booster"
            subtitle="Extra offer, if eligible"
          />
          <GuideStat
            icon={<LayersIcon className="size-4" />}
            title="Layering"
            subtitle="Extra payout linked to performance"
          />
        </div>
        <div className="flex items-start gap-2 rounded-lg border bg-muted/40 p-3 text-sm">
          <RotateCcwIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
          <p>
            <span className="font-medium">Renewal is separate. </span>
            Do not add the Renewal column into the first-year payout — it is a later-year
            payout only.
          </p>
        </div>
      </section>

      <section className="space-y-4">
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            See it in one example
          </p>
          <h2 className="text-lg font-semibold">Illustrative payout grid</h2>
          <p className="text-sm text-muted-foreground">
            Dummy example only — use it to learn how to read a real grid row.
          </p>
        </div>
        <Card>
          <CardContent className="space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <p className="text-sm font-medium">Plan A — Secure Term</p>
                <p className="text-xs text-muted-foreground">Term · Booster: 10% on eligible term plans for 5–10 PPT</p>
              </div>
            </div>
            <div className="overflow-x-auto rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow className="bg-muted/50 hover:bg-muted/50">
                    <TableHead>PPT</TableHead>
                    <TableHead>PT</TableHead>
                    <TableHead>Type</TableHead>
                    <TableHead className="text-right">Payout</TableHead>
                    <TableHead className="text-right">Renewal</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {EXAMPLE_ROWS.map((r) => (
                    <TableRow key={r.ppt}>
                      <TableCell className="font-medium">{r.ppt}</TableCell>
                      <TableCell className="text-muted-foreground">{r.pt}</TableCell>
                      <TableCell>{r.type}</TableCell>
                      <TableCell className="text-right tabular-nums font-medium">{r.base}</TableCell>
                      <TableCell className="text-right tabular-nums text-muted-foreground">{r.renewal}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
            <div className="grid gap-2 rounded-md bg-muted/40 p-3 text-sm sm:grid-cols-3">
              <div>
                <span className="font-medium">32% Base</span>
                <p className="text-xs text-muted-foreground">Row for PPT 5–9</p>
              </div>
              <div>
                <span className="font-medium">+ 10% Booster</span>
                <p className="text-xs text-muted-foreground">Plan is eligible for 5–10 PPT</p>
              </div>
              <div>
                <span className="font-medium">= 42% Before layering</span>
                <p className="text-xs text-muted-foreground">Layering added separately per month</p>
              </div>
            </div>
          </CardContent>
        </Card>
      </section>

      <section className="space-y-4">
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            How to calculate payout
          </p>
          <h2 className="text-lg font-semibold">Remember only these 3 steps</h2>
        </div>
        <div className="grid gap-3 sm:grid-cols-3">
          <GuideStep n={1} title="Match the row" desc="Insurer → Plan → PPT → PT." />
          <GuideStep n={2} title="Add booster" desc="Only if that exact row is eligible." />
          <GuideStep n={3} title="Add layering" desc="Only what is actually earned for the month." />
        </div>
        <div className="rounded-lg border bg-muted/40 p-3 text-sm">
          <span className="font-medium">Simple rule: </span>
          never quote a payout by looking only at the plan name. PPT, PT and booster
          conditions can change the number — always confirm against the exact grid row.
        </div>
      </section>

      <section className="space-y-4">
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Quick glossary
          </p>
          <h2 className="text-lg font-semibold">The terms you need most often</h2>
        </div>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {GLOSSARY_CORE.map((g) => (
            <div key={g.term} className="rounded-lg border p-3">
              <p className="text-sm font-semibold">{g.term}</p>
              {g.full && <p className="text-xs text-muted-foreground">{g.full}</p>}
              <p className="mt-1 text-sm text-muted-foreground">{g.desc}</p>
            </div>
          ))}
        </div>

        <details className="group rounded-lg border">
          <summary className="cursor-pointer list-none px-4 py-3 text-sm font-medium">
            More grid terms
            <span className="ml-2 text-xs text-muted-foreground group-open:hidden">
              (click to expand)
            </span>
          </summary>
          <div className="grid gap-3 border-t p-4 sm:grid-cols-2 lg:grid-cols-3">
            {GLOSSARY_MORE.map((g) => (
              <div key={g.term}>
                <p className="text-sm font-medium">{g.term}</p>
                <p className="text-sm text-muted-foreground">{g.desc}</p>
              </div>
            ))}
          </div>
        </details>
      </section>
    </div>
  );
}

function GuideStat({
  icon,
  title,
  subtitle,
}: {
  icon: React.ReactNode;
  title: string;
  subtitle: string;
}) {
  return (
    <div className="flex items-start gap-3 rounded-lg border p-3">
      <div className="flex size-8 shrink-0 items-center justify-center rounded-full bg-primary/10 text-primary">
        {icon}
      </div>
      <div>
        <p className="text-sm font-semibold">{title}</p>
        <p className="text-xs text-muted-foreground">{subtitle}</p>
      </div>
    </div>
  );
}

function GuideStep({ n, title, desc }: { n: number; title: string; desc: string }) {
  return (
    <div className="rounded-lg border p-3">
      <div className="flex size-6 items-center justify-center rounded-full bg-primary text-xs font-semibold text-primary-foreground">
        {n}
      </div>
      <p className="mt-2 text-sm font-semibold">{title}</p>
      <p className="text-xs text-muted-foreground">{desc}</p>
    </div>
  );
}
