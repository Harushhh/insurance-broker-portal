"use client";

import { useMemo, useState } from "react";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { FilterSelect, ALL } from "@/components/filter-select";
import { formatPayoutValue } from "@/lib/format";
import { canonicalType, formatRenewalSummary, getCanonicalTypes, getPpts } from "@/lib/filters";
import { categoryBadgeClass } from "@/lib/category-colors";
import type { PayoutRow } from "@/lib/types";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 25;

export function BestPayoutsTab({ rows }: { rows: PayoutRow[] }) {
  const [type, setType] = useState(ALL);
  const [ppt, setPpt] = useState(ALL);
  const [visible, setVisible] = useState(PAGE_SIZE);

  const types = useMemo(() => getCanonicalTypes(rows), [rows]);
  const pptOptions = useMemo(() => getPpts(rows, null, null), [rows]);

  const ranked = useMemo(() => {
    return rows
      .filter((r) => typeof r.Base_Payout_Yr1 === "number")
      .filter((r) => (type === ALL ? true : canonicalType(r.Category) === type))
      .filter((r) => (ppt === ALL ? true : r.PPT === ppt))
      .sort((a, b) => (b.Base_Payout_Yr1 as number) - (a.Base_Payout_Yr1 as number));
  }, [rows, type, ppt]);

  function handleTypeClick(value: string) {
    setType(value);
    setVisible(PAGE_SIZE);
  }

  const shown = ranked.slice(0, visible);

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-base font-semibold">Quick Filters</h2>
        <p className="text-sm text-muted-foreground">
          Ranked by base (year 1) payout — filter by plan type to find the strongest offer.
        </p>
      </div>

      <div className="-mx-1 flex flex-wrap gap-1.5 px-1">
        <button
          type="button"
          onClick={() => handleTypeClick(ALL)}
          className={cn(
            "shrink-0 rounded-full border px-3 py-1 text-xs font-medium transition-colors",
            type === ALL
              ? "border-primary bg-primary text-primary-foreground"
              : "border-border bg-background text-muted-foreground hover:bg-accent hover:text-accent-foreground"
          )}
        >
          All
        </button>
        {types.map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => handleTypeClick(t)}
            className={cn(
              "shrink-0 rounded-full border px-3 py-1 text-xs font-medium transition-colors",
              type === t
                ? "border-primary bg-primary text-primary-foreground"
                : "border-border bg-background text-muted-foreground hover:bg-accent hover:text-accent-foreground"
            )}
          >
            {t}
          </button>
        ))}
      </div>

      <div className="flex flex-wrap gap-3 rounded-lg border bg-card p-3">
        <FilterSelect
          label="PPT"
          value={ppt}
          onChange={(v) => {
            setPpt(v);
            setVisible(PAGE_SIZE);
          }}
          options={pptOptions}
          allLabel="All PPT"
        />
      </div>

      <p className="text-sm text-muted-foreground">
        {ranked.length} payout row{ranked.length === 1 ? "" : "s"} ranked
      </p>

      {ranked.length === 0 ? (
        <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
          No numeric payout rows match this filter.
        </div>
      ) : (
        <>
          <div className="overflow-x-auto rounded-lg border">
            <Table>
              <TableHeader>
                <TableRow className="bg-muted/50 hover:bg-muted/50">
                  <TableHead className="w-10">#</TableHead>
                  <TableHead>Insurer</TableHead>
                  <TableHead>Product</TableHead>
                  <TableHead>Type</TableHead>
                  <TableHead>PPT</TableHead>
                  <TableHead>PT</TableHead>
                  <TableHead className="text-right">Base (Yr 1)</TableHead>
                  <TableHead>Renewal years</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {shown.map((row, idx) => (
                  <TableRow key={row.Row_ID} className={idx < 3 ? "bg-primary/[0.03]" : undefined}>
                    <TableCell className="tabular-nums">
                      <RankBadge rank={idx + 1} />
                    </TableCell>
                    <TableCell className="font-medium whitespace-nowrap">
                      {row.InsurerDisplay}
                    </TableCell>
                    <TableCell className="max-w-[220px] whitespace-nowrap overflow-hidden text-ellipsis">
                      {row.Product_Name}
                      {row.Variant && (
                        <span className="text-muted-foreground"> · {row.Variant}</span>
                      )}
                    </TableCell>
                    <TableCell>
                      {row.Category ? (
                        <Badge
                          variant="outline"
                          className={cn("font-normal", categoryBadgeClass(canonicalType(row.Category)))}
                        >
                          {row.Category}
                        </Badge>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </TableCell>
                    <TableCell className="whitespace-nowrap">{row.PPT}</TableCell>
                    <TableCell className="whitespace-nowrap text-muted-foreground">
                      {row.PT}
                    </TableCell>
                    <TableCell className="text-right tabular-nums font-semibold">
                      {formatPayoutValue(row.Base_Payout_Yr1)}
                    </TableCell>
                    <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                      {formatRenewalSummary(row) ?? "—"}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
          {visible < ranked.length && (
            <div className="flex justify-center">
              <Button variant="outline" onClick={() => setVisible((v) => v + PAGE_SIZE)}>
                Show more payouts
              </Button>
            </div>
          )}
        </>
      )}

      <div className="rounded-lg border bg-muted/40 p-3 text-xs text-muted-foreground">
        <span className="font-medium text-foreground">Disclaimer: </span>
        Payouts shown here are base (year 1) payouts only. Applicable boosters, special
        conditions and layering are not reflected — check the Payout Guide tab for how to
        build the full offer, and open an insurer&apos;s notes in the Search Grid tab for
        booster/clawback conditions.
      </div>
    </div>
  );
}

function RankBadge({ rank }: { rank: number }) {
  if (rank > 3) {
    return <span className="text-muted-foreground">{rank}</span>;
  }
  const tier =
    rank === 1
      ? "bg-primary text-primary-foreground"
      : rank === 2
        ? "bg-primary/70 text-primary-foreground"
        : "bg-primary/40 text-primary-foreground";
  return (
    <span
      className={cn(
        "flex size-5 items-center justify-center rounded-full text-[11px] font-semibold",
        tier
      )}
    >
      {rank}
    </span>
  );
}
