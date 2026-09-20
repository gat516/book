// Session identity and CSRF token live in memory. The HttpOnly cookie is server-owned.
export interface Session { id: string; email: string; csrf_token: string; local?: boolean; legacy?: boolean }
let current: Session | null = null;
export function setSession(value: Session | null) { current = value; }
export function sessionHeaders(): Record<string, string> {
  if (current?.legacy) return { "X-Reader-ID": current.id };
  return current?.csrf_token ? { "X-CSRF-Token": current.csrf_token } : {};
}
export function sessionExpired() { setSession(null); window.dispatchEvent(new Event("book-session-expired")); }

export function hostedSession() { return current !== null && !current.local; }

// During a local rolling upgrade the old API has no session route. Production builds
// and actual authentication failures must never take this compatibility path.
export function canUseLegacyLocalSession(development: boolean, hostname: string, status: number): boolean {
  return development && ["localhost", "127.0.0.1", "[::1]", "::1"].includes(hostname) && status === 404;
}
export function legacyLocalSession(id: string): Session {
  return { id, email: "Local reader", csrf_token: "", local: true, legacy: true };
}
