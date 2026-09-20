// Session identity and CSRF token live in memory. The HttpOnly cookie is server-owned.
export interface Session { id: string; email: string; csrf_token: string; local?: boolean }
let current: Session | null = null;
export function setSession(value: Session | null) { current = value; }
export function sessionHeaders(): Record<string, string> {
  return current?.csrf_token ? { "X-CSRF-Token": current.csrf_token } : {};
}
export function sessionExpired() { setSession(null); window.dispatchEvent(new Event("book-session-expired")); }

export function hostedSession() { return current !== null && !current.local; }
