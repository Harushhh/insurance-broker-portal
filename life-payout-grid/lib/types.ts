export type PayoutValue = number | string | null;

export type PayoutRow = {
  Row_ID: number;
  Insurer: string;
  InsurerDisplay: string;
  InsurerCode: string;
  Category: string | null;
  Product_Name: string;
  Variant: string | null;
  PPT: string;
  PT: string;
  Base_Payout_Yr1: PayoutValue;
  Renewal_Yr2: PayoutValue;
  Renewal_Yr3: PayoutValue;
  Renewal_Yr4: PayoutValue;
  Renewal_Yr5: PayoutValue;
  Renewal_Yr6_Plus: PayoutValue;
  Row_Specific_Remarks: string | null;
  Sheet_Level_Notes_And_Conditions: string | null;
  All_Renewal_Years_Raw: string | null;
};

export type RenewalYearKey =
  | "Renewal_Yr2"
  | "Renewal_Yr3"
  | "Renewal_Yr4"
  | "Renewal_Yr5"
  | "Renewal_Yr6_Plus";

export const RENEWAL_YEAR_FIELDS: { key: RenewalYearKey; label: string }[] = [
  { key: "Renewal_Yr2", label: "Yr 2" },
  { key: "Renewal_Yr3", label: "Yr 3" },
  { key: "Renewal_Yr4", label: "Yr 4" },
  { key: "Renewal_Yr5", label: "Yr 5" },
  { key: "Renewal_Yr6_Plus", label: "Yr 6+" },
];

export type InsurerSummary = {
  code: string;
  name: string;
  sheet: string;
  rowCount: number;
  productCount: number;
};

export type GridMeta = {
  generatedAt: string;
  sourceFile: string;
  totalRows: number;
  totalProducts: number;
  insurers: InsurerSummary[];
  warnings: string[];
  sheetErrors: string[];
};

export type GridData = {
  meta: GridMeta;
  rows: PayoutRow[];
};
