import "server-only";
import crypto from "crypto";
import { cookies } from "next/headers";

const COOKIE_NAME = "lpg_session";
const SESSION_HOURS = 8;

export type PortalRole = "admin" | "viewer";

/**
 * There is no password for this app. Access is granted entirely by the
 * Django Insurance Portal: a logged-in user clicks a sidebar link there,
 * which signs a short-lived token with this same secret -- carrying their
 * username and a role ("admin" for the Can_Manage_Life_Payout_Grid-gated
 * Update Payout Rates link, "viewer" for the plain Life Payout Grid link
 * everyone gets -- and redirects here with it. We only ever check that
 * signature; the role travels inside it because Django already decided
 * which one applies before minting it.
 */
function getSecret(): string | null {
  return process.env.LIFE_PAYOUT_GRID_AUTH_SECRET || null;
}

function sign(secret: string, payload: string): string {
  return crypto.createHmac("sha256", secret).update(payload).digest("hex");
}

function timingSafeEqualStr(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  return crypto.timingSafeEqual(Buffer.from(a), Buffer.from(b));
}

function parseRole(raw: string | undefined): PortalRole | null {
  return raw === "admin" || raw === "viewer" ? raw : null;
}

type PortalSession = { username: string; role: PortalRole };

/**
 * Verifies a `username:expiryUnixSeconds:role.hexHmac` token minted by the
 * Django portal. Returns the username/role on success, or null if the
 * token is missing, malformed, expired, or doesn't verify against the
 * shared secret.
 */
export function verifyPortalToken(token: string | null | undefined): PortalSession | null {
  const secret = getSecret();
  if (!secret || !token) return null;

  const lastDot = token.lastIndexOf(".");
  if (lastDot === -1) return null;
  const payload = token.slice(0, lastDot);
  const sig = token.slice(lastDot + 1);
  if (!timingSafeEqualStr(sig, sign(secret, payload))) return null;

  const [username, expiryStr, roleStr] = payload.split(":");
  const expiry = Number(expiryStr);
  const role = parseRole(roleStr);
  if (!username || !Number.isFinite(expiry) || !role) return null;
  if (Date.now() > expiry * 1000) return null;

  return { username, role };
}

export async function createSession(username: string, role: PortalRole): Promise<void> {
  const secret = getSecret();
  if (!secret) {
    throw new Error("LIFE_PAYOUT_GRID_AUTH_SECRET is not configured.");
  }
  const expires = Date.now() + SESSION_HOURS * 60 * 60 * 1000;
  const payload = `${username}:${expires}:${role}`;
  const token = `${payload}.${sign(secret, payload)}`;
  const store = await cookies();
  store.set(COOKIE_NAME, token, {
    httpOnly: true,
    sameSite: "lax",
    path: "/",
    maxAge: SESSION_HOURS * 60 * 60,
  });
}

export async function clearSession(): Promise<void> {
  const store = await cookies();
  store.delete(COOKIE_NAME);
}

export async function getSession(): Promise<PortalSession | null> {
  const secret = getSecret();
  const store = await cookies();
  const token = store.get(COOKIE_NAME)?.value;
  if (!secret || !token) return null;

  const lastDot = token.lastIndexOf(".");
  if (lastDot === -1) return null;
  const payload = token.slice(0, lastDot);
  const sig = token.slice(lastDot + 1);
  if (!timingSafeEqualStr(sig, sign(secret, payload))) return null;

  const [username, expiryStr, roleStr] = payload.split(":");
  const expiry = Number(expiryStr);
  const role = parseRole(roleStr);
  if (!username || !Number.isFinite(expiry) || !role) return null;
  if (Date.now() > expiry) return null;

  return { username, role };
}

export async function isAuthenticated(): Promise<boolean> {
  return (await getSession()) !== null;
}

/** Stricter than isAuthenticated() -- true only for a session minted from the admin (Can_Manage_Life_Payout_Grid) handoff. Gates the update-grid page and its upload API. */
export async function isAdminAuthenticated(): Promise<boolean> {
  const session = await getSession();
  return session?.role === "admin";
}

/**
 * Where to send an unauthenticated (or wrong-role) visitor instead of
 * showing them a static "access denied" screen. Both portal paths are
 * login_required/page_access_required on the Django side, so this bounces
 * a visitor with a live Django session straight back in with a fresh
 * token, and one without gets Django's normal login-then-redirect-back
 * flow -- no separate login here, so there's nothing else this app could
 * offer instead. Returns null if INSURANCE_PORTAL_URL isn't configured.
 */
export function portalRedirectUrl(kind: "viewer" | "admin"): string | null {
  const base = process.env.INSURANCE_PORTAL_URL;
  if (!base) return null;
  const path = kind === "admin" ? "/life-payout-grid/admin/" : "/life-payout-grid/";
  return `${base.replace(/\/$/, "")}${path}`;
}
