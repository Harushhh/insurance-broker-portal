import { NextRequest, NextResponse } from "next/server";
import { checkPassword, createSession } from "@/lib/admin-auth";

export async function POST(request: NextRequest) {
  const body = await request.json().catch(() => null);
  const password = typeof body?.password === "string" ? body.password : "";

  if (!password || !checkPassword(password)) {
    return NextResponse.json(
      { ok: false, error: "Incorrect password." },
      { status: 401 }
    );
  }

  await createSession();
  return NextResponse.json({ ok: true });
}
