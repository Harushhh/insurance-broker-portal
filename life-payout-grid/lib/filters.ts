import { formatPayoutValue } from "./format";
import { RENEWAL_YEAR_FIELDS, type PayoutRow } from "./types";

export function uniqueSorted(values: (string | null | undefined)[]): string[] {
  const set = new Set<string>();
  for (const v of values) {
    if (v) set.add(v);
  }
  return Array.from(set).sort((a, b) => a.localeCompare(b));
}

export function getInsurers(rows: PayoutRow[]): string[] {
  return uniqueSorted(rows.map((r) => r.InsurerDisplay));
}

export function getProducts(rows: PayoutRow[], insurer: string | null): string[] {
  const scoped = insurer ? rows.filter((r) => r.InsurerDisplay === insurer) : rows;
  return uniqueSorted(scoped.map((r) => r.Product_Name));
}

export function getPpts(
  rows: PayoutRow[],
  insurer: string | null,
  product: string | null
): string[] {
  const scoped = rows.filter(
    (r) => (!insurer || r.InsurerDisplay === insurer) && (!product || r.Product_Name === product)
  );
  return uniqueSorted(scoped.map((r) => r.PPT));
}

export function getPts(
  rows: PayoutRow[],
  insurer: string | null,
  product: string | null,
  ppt: string | null
): string[] {
  const scoped = rows.filter(
    (r) =>
      (!insurer || r.InsurerDisplay === insurer) &&
      (!product || r.Product_Name === product) &&
      (!ppt || r.PPT === ppt)
  );
  return uniqueSorted(scoped.map((r) => r.PT));
}

export function getTypes(rows: PayoutRow[]): string[] {
  return uniqueSorted(rows.map((r) => r.Category));
}

export function matchesSearch(row: PayoutRow, query: string): boolean {
  if (!query.trim()) return true;
  const q = query.trim().toLowerCase();
  return (
    row.InsurerDisplay.toLowerCase().includes(q) ||
    row.Product_Name.toLowerCase().includes(q) ||
    (row.Variant ?? "").toLowerCase().includes(q) ||
    (row.Category ?? "").toLowerCase().includes(q) ||
    row.PPT.toLowerCase().includes(q) ||
    (row.Row_Specific_Remarks ?? "").toLowerCase().includes(q) ||
    (row.Sheet_Level_Notes_And_Conditions ?? "").toLowerCase().includes(q)
  );
}

/**
 * The source grid labels the same plan category dozens of different ways
 * across insurers (e.g. "Non Par", "Non-Par", "Non- PAR", "PAR(Trad)",
 * "Trad - Par"). This collapses those into a small, clean set of chips for
 * the Best Payouts quick filter, without touching the authentic label shown
 * in the Search Grid table.
 */
export function canonicalType(category: string | null): string | null {
  if (!category) return null;
  const t = category.toLowerCase();
  if (t.includes("tulip")) return "TULIP";
  if (t.includes("ulip")) return "ULIP";
  if (t.includes("annuity")) return "Annuity";
  if (t.includes("pension")) return "Pension";
  if (t.includes("term")) return "Term";
  if (t.includes("non par") || t.includes("non-par") || t.includes("non- par"))
    return "Non-Par";
  if (t.includes("par")) return "Par";
  if (t.includes("protection")) return "Protection";
  if (t.includes("trad")) return "Traditional / Investment";
  return category;
}

export function getCanonicalTypes(rows: PayoutRow[]): string[] {
  return uniqueSorted(rows.map((r) => canonicalType(r.Category)));
}

// --- Multi-year renewal helpers -------------------------------------------

export function populatedRenewalYears(row: PayoutRow) {
  return RENEWAL_YEAR_FIELDS.filter((f) => row[f.key] !== null);
}

/** Which of the 5 renewal-year columns actually have data anywhere in this
 * set of rows -- used to hide empty columns instead of rendering 5 mostly
 * blank ones for insurers that only ever report a single renewal year. */
export function visibleRenewalYearFields(rows: PayoutRow[]) {
  return RENEWAL_YEAR_FIELDS.filter((f) => rows.some((r) => r[f.key] !== null));
}

export function formatRenewalSummary(row: PayoutRow): string | null {
  const populated = populatedRenewalYears(row);
  if (populated.length === 0) return null;
  return populated.map((f) => `${f.label} ${formatPayoutValue(row[f.key])}`).join(" · ");
}

export type RawRenewalEntry = { label: string; value: string };

export function parseRenewalRaw(raw: string | null): RawRenewalEntry[] {
  if (!raw) return [];
  return raw
    .split(";")
    .map((part) => {
      const idx = part.indexOf(":");
      if (idx === -1) return { label: part.trim(), value: "" };
      return { label: part.slice(0, idx).trim(), value: part.slice(idx + 1).trim() };
    })
    .filter((e) => e.label);
}

/** The raw field always lists every renewal-like column the sheet has,
 * even ones that are blank for this particular row -- filter those out
 * before deciding whether there's anything worth showing. */
export function nonEmptyRenewalRaw(raw: string | null): RawRenewalEntry[] {
  return parseRenewalRaw(raw).filter((e) => e.value && e.value !== "—");
}

/** True when the source sheet had more distinct renewal-year columns than
 * fit the 5 clean slots (e.g. India_First's Year 2..Year 11+), so there's
 * extra detail worth surfacing beyond Renewal_Yr2..Yr6_Plus. */
export function hasRenewalOverflow(row: PayoutRow): boolean {
  const rawCount = nonEmptyRenewalRaw(row.All_Renewal_Years_Raw).length;
  return rawCount > populatedRenewalYears(row).length;
}
