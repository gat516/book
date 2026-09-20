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

// This is a compile-time development switch. Production builds always use sessions.
export function isLocalDevelopment(development: boolean, mode: string | undefined): boolean {
  return development && mode !== "hosted";
}
export function legacyLocalSession(id: string): Session {
  return { id, email: "Local reader", csrf_token: "", local: true, legacy: true };
}
