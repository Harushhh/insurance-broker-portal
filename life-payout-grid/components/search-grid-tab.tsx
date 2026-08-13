"use client";

import { useMemo, useState } from "react";
import { SearchIcon, XIcon, InfoIcon } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { FilterSelect, ALL } from "@/components/filter-select";
import { PayoutTable } from "@/components/payout-table";
import { getProducts, getPpts, getPts, matchesSearch, canonicalType } from "@/lib/filters";
import { categoryBadgeClass } from "@/lib/category-colors";
import type { InsurerSummary, PayoutRow } from "@/lib/types";
import { cn } from "@/lib/utils";

export function SearchGridTab({
  rows,
  insurerSummaries,
}: {
  rows: PayoutRow[];
  insurerSummaries: InsurerSummary[];
}) {
  const [query, setQuery] = useState("");
  const [insurer, setInsurer] = useState(ALL);
  const [product, setProduct] = useState(ALL);
  const [ppt, setPpt] = useState(ALL);
  const [pt, setPt] = useState(ALL);

  const chipInsurers = useMemo(
    () => [...insurerSummaries].sort((a, b) => b.rowCount - a.rowCount),
    [insurerSummaries]
  );

  const productOptions = useMemo(
    () => getProducts(rows, insurer === ALL ? null : insurer),
    [rows, insurer]
  );
  const pptOptions = useMemo(
    () =>
      getPpts(
        rows,
        insurer === ALL ? null : insurer,
        product === ALL ? null : product
      ),
    [rows, insurer, product]
  );
  const ptOptions = useMemo(
    () =>
      getPts(
        rows,
        insurer === ALL ? null : insurer,
        product === ALL ? null : product,
        ppt === ALL ? null : ppt
      ),
    [rows, insurer, product, ppt]
  );

  function handleInsurerChange(value: string) {
    setInsurer(value);
    setProduct(ALL);
    setPpt(ALL);
    setPt(ALL);
  }
  function handleProductChange(value: string) {
    setProduct(value);
    setPpt(ALL);
    setPt(ALL);
  }
  function handlePptChange(value: string) {
    setPpt(value);
    setPt(ALL);
  }

  function handleChipClick(insurerName: string) {
    if (insurer === insurerName) {
      handleInsurerChange(ALL);
    } else {
      handleInsurerChange(insurerName);
    }
  }

  const hasActiveFilters =
    Boolean(query.trim()) ||
    insurer !== ALL ||
    product !== ALL ||
    ppt !== ALL ||
    pt !== ALL;

  function clearAll() {
    setQuery("");
    handleInsurerChange(ALL);
  }

  const filteredRows = useMemo(() => {
    return rows.filter((r) => {
      if (insurer !== ALL && r.InsurerDisplay !== insurer) return false;
      if (product !== ALL && r.Product_Name !== product) return false;
      if (ppt !== ALL && r.PPT !== ppt) return false;
      if (pt !== ALL && r.PT !== pt) return false;
      if (!matchesSearch(r, query)) return false;
      return true;
    });
  }, [rows, insurer, product, ppt, pt, query]);

  const grouped = useMemo(() => {
    const byInsurer = new Map<string, Map<string, PayoutRow[]>>();
    const notesByInsurer = new Map<string, string>();
    for (const row of filteredRows) {
      if (!byInsurer.has(row.InsurerDisplay)) byInsurer.set(row.InsurerDisplay, new Map());
      const byProduct = byInsurer.get(row.InsurerDisplay)!;
      if (!byProduct.has(row.Product_Name)) byProduct.set(row.Product_Name, []);
      byProduct.get(row.Product_Name)!.push(row);
      if (row.Sheet_Level_Notes_And_Conditions && !notesByInsurer.has(row.InsurerDisplay)) {
        notesByInsurer.set(row.InsurerDisplay, row.Sheet_Level_Notes_And_Conditions);
      }
    }
    return Array.from(byInsurer.entries())
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([insurerName, productMap]) => ({
        insurer: insurerName,
        notes: notesByInsurer.get(insurerName) ?? null,
        products: Array.from(productMap.entries()).sort((a, b) =>
          a[0].localeCompare(b[0])
        ),
      }));
  }, [filteredRows]);

  const productCount = useMemo(
    () => new Set(filteredRows.map((r) => `${r.InsurerDisplay}::${r.Product_Name}`)).size,
    [filteredRows]
  );

  return (
    <div className="space-y-4">
      <div className="relative">
        <SearchIcon className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search insurer, plan, payout term or note…"
          className="pl-9 pr-9"
        />
        {query && (
          <button
            type="button"
            aria-label="Clear search"
            onClick={() => setQuery("")}
            className="absolute right-2.5 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
          >
            <XIcon className="size-4" />
          </button>
        )}
      </div>

      <div className="-mx-1 flex gap-1.5 overflow-x-auto px-1 pb-1">
        <button
          type="button"
          onClick={() => handleInsurerChange(ALL)}
          className={cn(
            "shrink-0 rounded-full border px-3 py-1 text-xs font-medium transition-colors",
            insurer === ALL
              ? "border-primary bg-primary text-primary-foreground"
              : "border-border bg-background text-muted-foreground hover:bg-accent hover:text-accent-foreground"
          )}
        >
          All
        </button>
        {chipInsurers.map((ins) => (
          <button
            key={ins.name}
            type="button"
            onClick={() => handleChipClick(ins.name)}
            className={cn(
              "shrink-0 rounded-full border px-3 py-1 text-xs font-medium transition-colors",
              insurer === ins.name
                ? "border-primary bg-primary text-primary-foreground"
                : "border-border bg-background text-muted-foreground hover:bg-accent hover:text-accent-foreground"
            )}
          >
            {ins.code}
          </button>
        ))}
      </div>

      <div className="flex flex-wrap gap-3 rounded-lg border bg-card p-3">
        <FilterSelect
          label="Insurer"
          value={insurer}
          onChange={handleInsurerChange}
          options={insurerSummaries.map((i) => i.name).sort((a, b) => a.localeCompare(b))}
          allLabel="All insurers"
        />
        <FilterSelect
          label="Product"
          value={product}
          onChange={handleProductChange}
          options={productOptions}
          allLabel="All products"
        />
        <FilterSelect
          label="PPT"
          value={ppt}
          onChange={handlePptChange}
          options={pptOptions}
          allLabel="All PPT"
        />
        <FilterSelect
          label="PT"
          value={pt}
          onChange={setPt}
          options={ptOptions}
          allLabel="All PT"
        />
        {hasActiveFilters && (
          <div className="flex items-end pb-0.5">
            <Button variant="ghost" size="sm" onClick={clearAll} className="gap-1 text-xs">
              <XIcon className="size-3.5" />
              Clear
            </Button>
          </div>
        )}
      </div>

      <p className="text-sm text-muted-foreground">
        {filteredRows.length} matching row{filteredRows.length === 1 ? "" : "s"} across{" "}
        {productCount} plan{productCount === 1 ? "" : "s"}
        {hasActiveFilters ? "" : " · tap an insurer to browse"}
      </p>

      {grouped.length === 0 ? (
        <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
          No payout rows match those filters. Try clearing a filter or search term.
        </div>
      ) : (
        <Accordion
          multiple
          key={hasActiveFilters ? grouped.map((g) => g.insurer).join("|") : "browse"}
          defaultValue={hasActiveFilters ? grouped.map((g) => g.insurer) : []}
          className="space-y-2"
        >
          {grouped.map((group) => (
            <AccordionItem
              key={group.insurer}
              value={group.insurer}
              className="rounded-lg border bg-card px-4"
            >
              <AccordionTrigger className="hover:no-underline">
                <span className="flex items-center gap-2 text-left">
                  <span className="font-medium">{group.insurer}</span>
                  <Badge variant="secondary" className="font-normal">
                    {group.products.length} plan{group.products.length === 1 ? "" : "s"}
                  </Badge>
                </span>
              </AccordionTrigger>
              <AccordionContent>
                <div className="space-y-4 pt-1">
                  {group.notes && <InsurerNotes notes={group.notes} />}
                  {group.products.map(([productName, productRows]) => {
                    const type = productRows.find((r) => r.Category)?.Category;
                    return (
                      <div key={productName} className="space-y-2">
                        <div className="flex flex-wrap items-center gap-2">
                          <h4 className="text-sm font-medium">{productName}</h4>
                          {type && (
                            <Badge
                              variant="outline"
                              className={cn("font-normal", categoryBadgeClass(canonicalType(type)))}
                            >
                              {type}
                            </Badge>
                          )}
                        </div>
                        <PayoutTable rows={productRows} />
                      </div>
                    );
                  })}
                </div>
              </AccordionContent>
            </AccordionItem>
          ))}
        </Accordion>
      )}
    </div>
  );
}

function InsurerNotes({ notes }: { notes: string }) {
  return (
    <details className="group rounded-lg border bg-muted/40">
      <summary className="flex cursor-pointer list-none items-start gap-2 px-3 py-2 text-xs font-medium text-muted-foreground">
        <InfoIcon className="mt-0.5 size-3.5 shrink-0" />
        <span className="flex-1">
          Notes &amp; conditions for this insurer
          <span className="ml-1.5 font-normal text-muted-foreground/70 group-open:hidden">
            (click to expand)
          </span>
        </span>
      </summary>
      <p className="border-t px-3 py-2 text-xs leading-relaxed text-muted-foreground">
        {notes}
      </p>
    </details>
  );
}
