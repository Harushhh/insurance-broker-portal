"use client";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { formatDate } from "@/lib/format";
import type { GridData } from "@/lib/types";
import { SearchIcon, TrendingUpIcon, BookOpenIcon, PercentIcon } from "lucide-react";
import { SearchGridTab } from "@/components/search-grid-tab";
import { BestPayoutsTab } from "@/components/best-payouts-tab";
import { PayoutGuideTab } from "@/components/payout-guide-tab";

export function Dashboard({ data }: { data: GridData }) {
  const { meta, rows } = data;

  return (
    <div className="flex min-h-screen flex-col">
      <header className="relative overflow-hidden border-b bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/80 sticky top-0 z-20">
        <div
          className="pointer-events-none absolute inset-x-0 top-0 h-1"
          style={{ background: "linear-gradient(90deg, var(--primary), #f3b98a)" }}
        />
        <div className="mx-auto max-w-6xl px-4 py-4 sm:px-6">
          <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
            <div className="flex items-center gap-2.5">
              <div
                className="flex size-8 shrink-0 items-center justify-center rounded-lg text-white shadow-sm sm:size-9"
                style={{ background: "linear-gradient(135deg, var(--primary), #f3b98a)" }}
              >
                <PercentIcon className="size-4 sm:size-[18px]" />
              </div>
              <h1 className="text-xl font-semibold tracking-tight sm:text-2xl">
                Searchable Payout Grid
              </h1>
            </div>
            <p className="text-xs text-muted-foreground sm:text-sm">
              {meta.sourceFile ? `Effective ${meta.sourceFile}` : ""}
              {meta.sourceFile && " · "}
              Internal circulation
              {meta.generatedAt && (
                <span className="hidden sm:inline">
                  {" "}
                  · Updated {formatDate(meta.generatedAt)}
                </span>
              )}
            </p>
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            {meta.insurers.length} insurers · {meta.totalProducts} plans ·{" "}
            {meta.totalRows} payout rows
          </p>
        </div>
      </header>

      <Tabs defaultValue="search" className="flex flex-1 flex-col gap-0">
        <div className="border-b bg-background sticky top-[73px] sm:top-[81px] z-10">
          <div className="mx-auto max-w-6xl px-4 sm:px-6">
            <TabsList className="h-auto w-full justify-start gap-1 rounded-none border-0 bg-transparent p-0">
              <TabsTrigger
                value="search"
                className="gap-1.5 rounded-none border-b-2 border-transparent px-3 py-2.5 data-active:border-primary data-active:bg-transparent data-active:text-primary data-active:shadow-none"
              >
                <SearchIcon className="size-4" />
                Search Grid
              </TabsTrigger>
              <TabsTrigger
                value="best"
                className="gap-1.5 rounded-none border-b-2 border-transparent px-3 py-2.5 data-active:border-primary data-active:bg-transparent data-active:text-primary data-active:shadow-none"
              >
                <TrendingUpIcon className="size-4" />
                Best Payout
              </TabsTrigger>
              <TabsTrigger
                value="guide"
                className="gap-1.5 rounded-none border-b-2 border-transparent px-3 py-2.5 data-active:border-primary data-active:bg-transparent data-active:text-primary data-active:shadow-none"
              >
                <BookOpenIcon className="size-4" />
                Payout Guide
              </TabsTrigger>
            </TabsList>
          </div>
        </div>

        <div className="mx-auto w-full max-w-6xl flex-1 px-4 py-6 sm:px-6">
          <TabsContent value="search" className="mt-0">
            <SearchGridTab rows={rows} insurerSummaries={meta.insurers} />
          </TabsContent>
          <TabsContent value="best" className="mt-0">
            <BestPayoutsTab rows={rows} />
          </TabsContent>
          <TabsContent value="guide" className="mt-0">
            <PayoutGuideTab />
          </TabsContent>
        </div>
      </Tabs>

      <footer className="border-t py-4">
        <div className="mx-auto max-w-6xl px-4 text-center text-xs text-muted-foreground sm:px-6">
          For internal broker use only · Rates are indicative — confirm with the insurer before committing to a client.
        </div>
      </footer>
    </div>
  );
}
