import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { formatPayoutValue } from "@/lib/format";
import {
  hasRenewalOverflow,
  nonEmptyRenewalRaw,
  visibleRenewalYearFields,
} from "@/lib/filters";
import type { PayoutRow } from "@/lib/types";

export function PayoutTable({ rows }: { rows: PayoutRow[] }) {
  const renewalFields = visibleRenewalYearFields(rows);
  const showVariant = rows.some((r) => r.Variant);
  const showRemarks = rows.some((r) => r.Row_Specific_Remarks || hasRenewalOverflow(r));

  return (
    <div className="overflow-x-auto rounded-md border">
      <Table>
        <TableHeader>
          <TableRow className="bg-muted/50 hover:bg-muted/50">
            {showVariant && <TableHead className="whitespace-nowrap">Variant</TableHead>}
            <TableHead className="whitespace-nowrap">PPT</TableHead>
            <TableHead className="whitespace-nowrap">PT</TableHead>
            <TableHead className="whitespace-nowrap text-right">
              Base (Yr 1)
            </TableHead>
            {renewalFields.map((f) => (
              <TableHead key={f.key} className="whitespace-nowrap text-right">
                {f.label}
              </TableHead>
            ))}
            {showRemarks && <TableHead className="min-w-[160px]">Notes</TableHead>}
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((row) => (
            <TableRow key={row.Row_ID}>
              {showVariant && (
                <TableCell className="whitespace-nowrap text-muted-foreground">
                  {row.Variant ?? "—"}
                </TableCell>
              )}
              <TableCell className="whitespace-nowrap font-medium">
                {row.PPT}
              </TableCell>
              <TableCell className="whitespace-nowrap text-muted-foreground">
                {row.PT}
              </TableCell>
              <TableCell className="whitespace-nowrap text-right tabular-nums font-semibold">
                {formatPayoutValue(row.Base_Payout_Yr1)}
              </TableCell>
              {renewalFields.map((f) => (
                <TableCell
                  key={f.key}
                  className="whitespace-nowrap text-right tabular-nums text-muted-foreground"
                >
                  {formatPayoutValue(row[f.key])}
                </TableCell>
              ))}
              {showRemarks && (
                <TableCell className="text-xs text-muted-foreground">
                  <RowDetail row={row} />
                </TableCell>
              )}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

function RowDetail({ row }: { row: PayoutRow }) {
  const overflow = hasRenewalOverflow(row);
  if (!row.Row_Specific_Remarks && !overflow) return null;
  return (
    <div className="space-y-0.5">
      {row.Row_Specific_Remarks && <p>{row.Row_Specific_Remarks}</p>}
      {overflow && (
        <p>
          <span className="font-medium text-foreground">Full schedule: </span>
          {nonEmptyRenewalRaw(row.All_Renewal_Years_Raw)
            .map((e) => `${e.label} ${e.value}`)
            .join(", ")}
        </p>
      )}
    </div>
  );
}
