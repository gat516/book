// Repair authority is deliberately separate from the unauthenticated reader id and from
// the ingest service's broader internal token. Session storage ends that authority with
// the tab and keeps it off unrelated API requests (spec §0.3).
const KEY = "repair-operator-token";

export function operatorToken(): string {
  try {
    return sessionStorage.getItem(KEY) ?? "";
  } catch {
    return "";
  }
}

export function setOperatorToken(token: string): void {
  try {
    if (token) sessionStorage.setItem(KEY, token);
    else sessionStorage.removeItem(KEY);
  } catch {
    // The next status response remains the source of truth about authorization.
  }
}

export function clearOperatorToken(): void {
  setOperatorToken("");
}
