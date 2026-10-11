// Keycloak login: OpenID Connect authorization code flow with PKCE (public client "web").
// Tokens live in sessionStorage (cleared when the tab closes); the access token is refreshed a
// minute before it expires. Display-only claims are read from the token without verifying it:
// the API verifies every token it receives.

const ISSUER = import.meta.env.VITE_OIDC_ISSUER ?? "http://localhost:8080/realms/wells";
const CLIENT_ID = "web";
const STORE = "oidc";
const PENDING = "oidc_pending";

type Tokens = { access_token: string; refresh_token?: string; expires_at: number };

const redirectUri = () => `${location.origin}/`;

function b64url(bytes: Uint8Array): string {
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function random(n = 32): string {
  return b64url(crypto.getRandomValues(new Uint8Array(n)));
}

function save(t: { access_token: string; refresh_token?: string; expires_in: number }): void {
  const tokens: Tokens = { access_token: t.access_token, refresh_token: t.refresh_token, expires_at: Date.now() + t.expires_in * 1000 };
  sessionStorage.setItem(STORE, JSON.stringify(tokens));
  sessionStorage.setItem("access_token", t.access_token); // read by the live view
}

function load(): Tokens | null {
  try {
    return JSON.parse(sessionStorage.getItem(STORE) ?? "null") as Tokens | null;
  } catch {
    return null;
  }
}

/** Sign in; `signup` opens Keycloak's registration form instead (same PKCE flow). */
export async function login(signup = false): Promise<void> {
  const verifier = random(48);
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
  const state = random(16);
  sessionStorage.setItem(PENDING, JSON.stringify({ verifier, state, back: location.hash || "#/" }));
  const q = new URLSearchParams({
    client_id: CLIENT_ID,
    response_type: "code",
    scope: "openid",
    redirect_uri: redirectUri(),
    code_challenge: b64url(digest),
    code_challenge_method: "S256",
    state,
  });
  location.assign(`${ISSUER}/protocol/openid-connect/${signup ? "registrations" : "auth"}?${q}`);
}

async function tokenRequest(body: Record<string, string>): Promise<void> {
  const r = await fetch(`${ISSUER}/protocol/openid-connect/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ client_id: CLIENT_ID, ...body }),
  });
  if (!r.ok) throw new Error(`token endpoint: ${r.status}`);
  save(await r.json());
}

/** Finish a login redirect (?code=&state=). True when this page load completed one. */
export async function completeLogin(): Promise<boolean> {
  const p = new URLSearchParams(location.search);
  const code = p.get("code");
  if (!code) return false;
  const pending = JSON.parse(sessionStorage.getItem(PENDING) ?? "null") as { verifier: string; state: string; back: string } | null;
  sessionStorage.removeItem(PENDING);
  history.replaceState(null, "", `/${pending?.back ?? "#/"}`);
  // replaceState fires no hashchange: tell the router (if it already listens), so the user lands
  // where they signed in. useRoute also re-reads the hash when it starts listening.
  window.dispatchEvent(new HashChangeEvent("hashchange"));
  if (!pending || pending.state !== p.get("state")) throw new Error("login state mismatch");
  await tokenRequest({ grant_type: "authorization_code", code, redirect_uri: redirectUri(), code_verifier: pending.verifier });
  return true;
}

export function token(): string | null {
  const t = load();
  return t && t.expires_at > Date.now() ? t.access_token : null;
}

export function startRefresh(): () => void {
  const timer = setInterval(() => {
    const t = load();
    if (t?.refresh_token && t.expires_at - Date.now() < 60_000) {
      tokenRequest({ grant_type: "refresh_token", refresh_token: t.refresh_token }).catch(() => logoutLocal());
    }
  }, 15_000);
  return () => clearInterval(timer);
}

function logoutLocal(): void {
  sessionStorage.removeItem(STORE);
  sessionStorage.removeItem("access_token");
}

export function logout(): void {
  logoutLocal();
  const q = new URLSearchParams({ client_id: CLIENT_ID, post_logout_redirect_uri: redirectUri() });
  location.assign(`${ISSUER}/protocol/openid-connect/logout?${q}`);
}

export type Who = { name: string; roles: string[] };

export function who(): Who | null {
  const t = token();
  if (!t) return null;
  try {
    const claims = JSON.parse(atob(t.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
    return { name: claims.preferred_username ?? claims.sub, roles: claims.realm_access?.roles ?? [] };
  } catch {
    return null;
  }
}

/** Mirrors PERMISSIONS in apps/api/src/api/auth.py (the API enforces it; this only hides buttons). */
export const PERMISSIONS = {
  plan: ["planner"],
  operate: ["wellsite_engineer", "superintendent"],
  review: ["data_steward"],
  admin: ["admin"],
} as const;
export type Permission = keyof typeof PERMISSIONS;
/** `viewer` reads and does nothing else (self-registered users start with it). */
export const APP_ROLES: readonly string[] = [...Object.values(PERMISSIONS).flat(), "viewer"];
export const can = (w: Who | null, p: Permission) => !!w && w.roles.some((r) => (PERMISSIONS[p] as readonly string[]).includes(r));
