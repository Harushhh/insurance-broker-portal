import { NextRequest, NextResponse } from "next/server";
import { verifyPortalToken, createSession } from "@/lib/admin-auth";

/**
 * Entry point for the handoff link the Django portal generates. Cookies
 * can only be set from a Route Handler or Server Action in the App
 * Router -- not from a plain page render -- so token verification lives
 * here rather than in app/admin/update-grid/page.tsx.
 */
export async function GET(request: NextRequest) {
  const token = request.nextUrl.searchParams.get("token");
  const username = verifyPortalToken(token);

  const destination = new URL("/admin/update-grid", request.url);
  const response = NextResponse.redirect(destination);

  if (username) {
    await createSession(username);
  }
  // Whether or not the token was valid, land on the clean admin URL --
  // isAuthenticated() there decides what's shown. This also keeps the
  // one-time token from lingering in the address bar either way.
  return response;
}
