import "server-only";
import fs from "fs";
import path from "path";
import type { GridData } from "./types";

const DATA_PATH = path.join(process.cwd(), "data", "payout_data.json");

export function loadGridData(): GridData {
  try {
    const raw = fs.readFileSync(DATA_PATH, "utf-8");
    return JSON.parse(raw) as GridData;
  } catch {
    return {
      meta: {
        generatedAt: "",
        sourceFile: "",
        totalRows: 0,
        totalProducts: 0,
        insurers: [],
        warnings: [],
        sheetErrors: [],
      },
      rows: [],
    };
  }
}
