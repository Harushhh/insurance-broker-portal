"use client";

import { useRef, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import {
  UploadCloudIcon,
  FileSpreadsheetIcon,
  CheckCircle2Icon,
  AlertTriangleIcon,
  XCircleIcon,
  LogOutIcon,
  ArrowLeftIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { formatDate } from "@/lib/format";
import type { GridMeta } from "@/lib/types";
import { cn } from "@/lib/utils";

type UploadState =
  | { status: "idle" }
  | { status: "uploading"; fileName: string }
  | {
      status: "success";
      fileName: string;
      rows: number;
      insurers: number;
      warnings: string[];
      sheetErrors: string[];
    }
  | { status: "error"; fileName: string; error: string; warnings?: string[]; sheetErrors?: string[] };

export function AdminUpload({ meta }: { meta: GridMeta }) {
  const router = useRouter();
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [state, setState] = useState<UploadState>({ status: "idle" });

  async function handleFile(file: File) {
    if (!file.name.toLowerCase().endsWith(".xlsx")) {
      setState({
        status: "error",
        fileName: file.name,
        error: "Only .xlsx workbooks are supported.",
      });
      return;
    }

    setState({ status: "uploading", fileName: file.name });
    const formData = new FormData();
    formData.append("file", file);

    try {
      const res = await fetch("/api/admin/upload", { method: "POST", body: formData });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        setState({
          status: "error",
          fileName: file.name,
          error: data.error ?? "Upload failed for an unknown reason.",
          warnings: data.warnings,
          sheetErrors: data.sheetErrors,
        });
        return;
      }
      setState({
        status: "success",
        fileName: file.name,
        rows: data.rows,
        insurers: data.insurers,
        warnings: data.warnings ?? [],
        sheetErrors: data.sheetErrors ?? [],
      });
      router.refresh();
    } catch {
      setState({
        status: "error",
        fileName: file.name,
        error: "Could not reach the server while uploading.",
      });
    }
  }

  function onDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragging(false);
    const file = e.dataTransfer.files?.[0];
    if (file) handleFile(file);
  }

  async function handleLogout() {
    await fetch("/api/admin/logout", { method: "POST" });
    router.refresh();
  }

  const busy = state.status === "uploading";

  return (
    <div className="mx-auto max-w-2xl px-4 py-10">
      <div className="mb-6 flex items-center justify-between">
        <Link
          href="/"
          className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeftIcon className="size-4" />
          Back to dashboard
        </Link>
        <Button variant="ghost" size="sm" onClick={handleLogout} className="gap-1.5 text-xs">
          <LogOutIcon className="size-3.5" />
          Sign out
        </Button>
      </div>

      <h1 className="text-xl font-semibold">Update Payout Grid</h1>
      <p className="mt-1 text-sm text-muted-foreground">
        Upload the new month&apos;s Life Grid workbook. It&apos;s parsed with the same logic used
        to build the dashboard, and the live data updates instantly on success.
      </p>

      <Card className="mt-6">
        <CardHeader>
          <CardTitle className="text-sm">Current live data</CardTitle>
          <CardDescription>What the dashboard is showing right now.</CardDescription>
        </CardHeader>
        <CardContent>
          <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
            <div>
              <dt className="text-xs text-muted-foreground">Source</dt>
              <dd className="font-medium">{meta.sourceFile || "—"}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">Updated</dt>
              <dd className="font-medium">{meta.generatedAt ? formatDate(meta.generatedAt) : "—"}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">Insurers</dt>
              <dd className="font-medium">{meta.insurers.length}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">Payout rows</dt>
              <dd className="font-medium">{meta.totalRows}</dd>
            </div>
          </dl>
        </CardContent>
      </Card>

      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        onClick={() => !busy && inputRef.current?.click()}
        className={cn(
          "mt-6 flex flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed p-10 text-center transition-colors",
          busy ? "cursor-wait opacity-60" : "cursor-pointer",
          dragging ? "border-primary bg-primary/5" : "border-border hover:bg-accent/50"
        )}
      >
        <UploadCloudIcon className="size-8 text-muted-foreground" />
        <p className="text-sm font-medium">
          {busy ? "Processing…" : "Drag and drop the new .xlsx file here"}
        </p>
        <p className="text-xs text-muted-foreground">or click to browse</p>
        <input
          ref={inputRef}
          type="file"
          accept=".xlsx"
          className="hidden"
          disabled={busy}
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) handleFile(file);
            e.target.value = "";
          }}
        />
      </div>

      {state.status === "success" && (
        <div className="mt-4 rounded-lg border border-green-600/30 bg-green-600/5 p-4">
          <div className="flex items-start gap-2">
            <CheckCircle2Icon className="mt-0.5 size-4 shrink-0 text-green-600" />
            <div className="text-sm">
              <p className="font-medium">
                {state.fileName} parsed successfully — the dashboard is now live with the new data.
              </p>
              <p className="mt-0.5 text-muted-foreground">
                {state.rows} payout rows across {state.insurers} insurers.
              </p>
            </div>
          </div>
          {state.warnings.length > 0 && (
            <WarningList title="Warnings (data still updated)" items={state.warnings} />
          )}
        </div>
      )}

      {state.status === "error" && (
        <div className="mt-4 rounded-lg border border-destructive/30 bg-destructive/5 p-4">
          <div className="flex items-start gap-2">
            <XCircleIcon className="mt-0.5 size-4 shrink-0 text-destructive" />
            <div className="text-sm">
              <p className="font-medium">Couldn&apos;t update the grid from {state.fileName}</p>
              <p className="mt-0.5 text-muted-foreground">{state.error}</p>
              <p className="mt-2 text-xs text-muted-foreground">
                The live dashboard still has last month&apos;s data — nothing was overwritten.
              </p>
            </div>
          </div>
          {state.sheetErrors && state.sheetErrors.length > 0 && (
            <WarningList title="Sheet errors" items={state.sheetErrors} tone="destructive" />
          )}
          {state.warnings && state.warnings.length > 0 && (
            <WarningList title="Warnings" items={state.warnings} />
          )}
        </div>
      )}

      <div className="mt-8 flex items-start gap-2 rounded-lg border bg-muted/40 p-3 text-xs text-muted-foreground">
        <FileSpreadsheetIcon className="mt-0.5 size-4 shrink-0" />
        <p>
          Expects the same 16-sheet workbook layout (TATA_AIA, Bajaj, Shriram, Digit, HDFC, Kotak,
          India_First, Bharti_Axa, ABLI, ICICI_Pru, AxisMax, Ageas Federal, Generali(FGI), PNB, SUD,
          Pramerica). Header rows and column names can move around — the parser locates them by
          keyword. If a sheet&apos;s structure changes too much, upload is blocked and last month&apos;s
          data stays live.
        </p>
      </div>
    </div>
  );
}

function WarningList({
  title,
  items,
  tone = "warning",
}: {
  title: string;
  items: string[];
  tone?: "warning" | "destructive";
}) {
  return (
    <div className="mt-3 border-t pt-3">
      <p
        className={cn(
          "flex items-center gap-1.5 text-xs font-medium",
          tone === "destructive" ? "text-destructive" : "text-amber-600"
        )}
      >
        <AlertTriangleIcon className="size-3.5" />
        {title}
      </p>
      <ul className="mt-1.5 list-inside list-disc space-y-0.5 text-xs text-muted-foreground">
        {items.map((w, i) => (
          <li key={i}>{w}</li>
        ))}
      </ul>
    </div>
  );
}
