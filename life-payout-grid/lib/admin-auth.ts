import "server-only";
import crypto from "crypto";
import { cookies } from "next/headers";

const COOKIE_NAME = "lpg_admin";
const SESSION_HOURS = 8;

/**
 * There is no password for this app. Access is granted entirely by the
 * Django Insurance Portal: a user with the "Can_Manage_Life_Payout_Grid"
 * permission clicks a sidebar link there, which signs a short-lived token
 * with this same secret and redirects here with it. We only ever check
 * that signature -- if you can produce a validly-signed token, the portal
 * already decided you're allowed in.
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

/**
 * Verifies a `username:expiryUnixSeconds.hexHmac` token minted by the
 * Django portal. Returns the username on success so callers can log who
 * signed in, or null if the token is missing, malformed, expired, or
 * doesn't verify against the shared secret.
 */
export function verifyPortalToken(token: string | null | undefined): string | null {
  const secret = getSecret();
  if (!secret || !token) return null;

  const lastDot = token.lastIndexOf(".");
  if (lastDot === -1) return null;
  const payload = token.slice(0, lastDot);
  const sig = token.slice(lastDot + 1);
  if (!timingSafeEqualStr(sig, sign(secret, payload))) return null;

  const [username, expiryStr] = payload.split(":");
  const expiry = Number(expiryStr);
  if (!username || !Number.isFinite(expiry)) return null;
  if (Date.now() > expiry * 1000) return null;

  return username;
}

export async function createSession(username: string): Promise<void> {
  const secret = getSecret();
  if (!secret) {
    throw new Error("LIFE_PAYOUT_GRID_AUTH_SECRET is not configured.");
  }
  const expires = Date.now() + SESSION_HOURS * 60 * 60 * 1000;
  const payload = `${username}:${expires}`;
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

export async function getSessionUser(): Promise<string | null> {
  const secret = getSecret();
  const store = await cookies();
  const token = store.get(COOKIE_NAME)?.value;
  if (!secret || !token) return null;

  const lastDot = token.lastIndexOf(".");
  if (lastDot === -1) return null;
  const payload = token.slice(0, lastDot);
  const sig = token.slice(lastDot + 1);
  if (!timingSafeEqualStr(sig, sign(secret, payload))) return null;

  const [username, expiryStr] = payload.split(":");
  const expiry = Number(expiryStr);
  if (!username || !Number.isFinite(expiry)) return null;
  if (Date.now() > expiry) return null;

  return username;
}

export async function isAuthenticated(): Promise<boolean> {
  return (await getSessionUser()) !== null;
}
