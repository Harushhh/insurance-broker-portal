import { NextRequest, NextResponse } from "next/server";
import { execFile } from "child_process";
import fs from "fs/promises";
import path from "path";
import { isAdminAuthenticated } from "@/lib/admin-auth";

const PROJECT_ROOT = process.cwd();
// Local dev runs on Windows (venv/Scripts/python.exe); the Railway
// container is Linux (venv/bin/python3, built by the Dockerfile). An
// explicit PYTHON_PATH env var overrides both if needed.
const PYTHON_PATH =
  process.env.PYTHON_PATH ||
  (process.platform === "win32"
    ? path.join(PROJECT_ROOT, "scripts", "venv", "Scripts", "python.exe")
    : path.join(PROJECT_ROOT, "scripts", "venv", "bin", "python3"));
const PARSER_PATH = path.join(PROJECT_ROOT, "scripts", "extract_master.py");
const DATA_PATH = path.join(PROJECT_ROOT, "data", "payout_data.json");
const UPLOADS_DIR = path.join(PROJECT_ROOT, "data", "uploads");

type ParserResult = {
  ok: boolean;
  rows?: number;
  insurers?: number;
  warnings?: string[];
  sheetErrors?: string[];
  error?: string;
};

function runParser(inputPath: string, outputPath: string, sourceLabel: string) {
  return new Promise<{ stdout: string; failed: boolean }>((resolve) => {
    execFile(
      PYTHON_PATH,
      [PARSER_PATH, inputPath, outputPath, "--source-name", sourceLabel],
      { timeout: 60_000 },
      (error, stdout) => {
        resolve({ stdout, failed: Boolean(error) });
      }
    );
  });
}

export async function POST(request: NextRequest) {
  if (!(await isAdminAuthenticated())) {
    return NextResponse.json({ ok: false, error: "Not authenticated." }, { status: 401 });
  }

  let formData: FormData;
  try {
    formData = await request.formData();
  } catch {
    return NextResponse.json({ ok: false, error: "Could not read the upload." }, { status: 400 });
  }

  const file = formData.get("file");
  if (!(file instanceof File)) {
    return NextResponse.json({ ok: false, error: "No file was uploaded." }, { status: 400 });
  }
  if (!file.name.toLowerCase().endsWith(".xlsx")) {
    return NextResponse.json(
      { ok: false, error: "Only .xlsx workbooks are supported." },
      { status: 400 }
    );
  }

  await fs.mkdir(UPLOADS_DIR, { recursive: true });

  const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
  const safeName = file.name.replace(/[^a-zA-Z0-9'_.() -]/g, "_");
  const uploadedPath = path.join(UPLOADS_DIR, `${timestamp}__${safeName}`);
  const tempOutputPath = path.join(UPLOADS_DIR, `${timestamp}__parsed.json`);

  const buffer = Buffer.from(await file.arrayBuffer());
  await fs.writeFile(uploadedPath, buffer);

  const sourceLabel =
    safeName
      .replace(/\.xlsx$/i, "")
      .replace(/^Life[_ ]?Grid[_ ]?/i, "")
      .trim() || safeName;

  const { stdout, failed } = await runParser(uploadedPath, tempOutputPath, sourceLabel);

  let result: ParserResult | null = null;
  try {
    result = JSON.parse(stdout) as ParserResult;
  } catch {
    result = null;
  }

  if (!result) {
    return NextResponse.json(
      {
        ok: false,
        error: failed
          ? "The parser crashed and didn't return a readable response. The file format may be too different from what the parser expects."
          : "The parser returned an unreadable response.",
      },
      { status: 500 }
    );
  }

  if (!result.ok) {
    return NextResponse.json(result, { status: 422 });
  }

  await fs.copyFile(tempOutputPath, DATA_PATH);
  await fs.unlink(tempOutputPath).catch(() => {});

  return NextResponse.json(result);
}
