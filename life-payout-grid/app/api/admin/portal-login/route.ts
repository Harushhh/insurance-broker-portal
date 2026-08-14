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

  if (username) {
    await createSession(username);
  }
  // Whether or not the token was valid, land on the clean admin URL --
  // isAuthenticated() there decides what's shown. This also keeps the
  // one-time token from lingering in the address bar either way.
  //
  // This is a relative Location header, not NextResponse.redirect(new
  // URL(path, request.url)) -- behind Railway's proxy, request.url's
  // host reflects the container's own address, not the public domain,
  // which sent this redirect to localhost instead of the real host. A
  // relative Location is resolved by the browser against the URL it
  // actually navigated to, so it's correct regardless of what the
  // container thinks its own host is.
  return new NextResponse(null, {
    status: 307,
    headers: { Location: "/admin/update-grid" },
  });
}
