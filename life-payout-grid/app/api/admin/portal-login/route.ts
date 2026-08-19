import { NextRequest, NextResponse } from "next/server";
import { verifyPortalToken, createSession } from "@/lib/admin-auth";

/**
 * Entry point for both handoff links the Django portal generates (the
 * plain "Life Payout Grid" link and the Can_Manage_Life_Payout_Grid-gated
 * "Update Payout Rates" one) -- the token's embedded role is what tells
 * them apart. Cookies can only be set from a Route Handler or Server
 * Action in the App Router -- not from a plain page render -- so token
 * verification lives here rather than in the destination pages.
 */
export async function GET(request: NextRequest) {
  const token = request.nextUrl.searchParams.get("token");
  const verified = verifyPortalToken(token);

  if (verified) {
    await createSession(verified.username, verified.role);
  }
  // Land admins on the edit page and everyone else (including a failed
  // verification, where we don't know which was intended) on the plain
  // grid -- each destination's own auth check decides what's actually
  // shown. This also keeps the one-time token from lingering in the
  // address bar either way.
  //
  // This is a relative Location header, not NextResponse.redirect(new
  // URL(path, request.url)) -- behind Railway's proxy, request.url's
  // host reflects the container's own address, not the public domain,
  // which sent this redirect to localhost instead of the real host. A
  // relative Location is resolved by the browser against the URL it
  // actually navigated to, so it's correct regardless of what the
  // container thinks its own host is.
  const destination = verified?.role === "admin" ? "/admin/update-grid" : "/";
  return new NextResponse(null, {
    status: 307,
    headers: { Location: destination },
  });
}
