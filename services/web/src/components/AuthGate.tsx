import { readerId } from "../readerId";
import { useEffect, useState, type ReactNode } from "react";
import { setSession, sessionHeaders, type Session, isLocalDevelopment, legacyLocalSession } from "../session";
import { LandingPage } from "./LandingPage";
import { SiteFooter, SiteHeader } from "./SiteHeader";

export function AuthGate({ children }: { children: ReactNode }) {
  const local = isLocalDevelopment(import.meta.env.DEV, import.meta.env.VITE_BOOK_MODE);
  if (local) setSession(legacyLocalSession(readerId()));
  const [account, setAccount] = useState<Session | null>(() => {
    if (!local) return null;
    const session = legacyLocalSession(readerId());
    setSession(session);
    return session;
  });
  const [loading, setLoading] = useState(!local);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (local) return;
    let alive = true;
    const expired = () => { setSession(null); setAccount(null); setError("Your session ended. Sign in again."); };
    window.addEventListener("book-session-expired", expired);
    fetch("/api/auth/session", { credentials: "same-origin" }).then(async response => {
      if (response.status === 401) return null;
      if (!response.ok) throw new Error("Sign-in is temporarily unavailable.");
      return response.json() as Promise<Session>;
    }).then(session => { if (alive) { setSession(session); setAccount(session); } })
      .catch(e => { if (alive) setError(e instanceof Error ? e.message : "Could not connect."); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; window.removeEventListener("book-session-expired", expired); };
  }, [local]);
  async function logout(all = false) {
    setBusy(true); setError(null);
    try {
      const response = await fetch(`/api/auth/${all ? "revoke-sessions" : "logout"}`, { method: "POST", headers: sessionHeaders() });
      if (!response.ok) throw new Error("Could not sign out. Please retry.");
      setSession(null); setAccount(null); window.history.replaceState({}, "", "/");
    } catch { setError("Could not sign out. Check your connection and try again."); }
    finally { setBusy(false); }
  }
  async function deleteAccount() {
    if (window.prompt("This permanently deletes your library and saved keys. Type DELETE to continue.") !== "DELETE") return;
    setBusy(true); setError(null);
    try {
      const response = await fetch("/api/auth/account", { method: "DELETE", headers: sessionHeaders() });
      if (!response.ok) throw new Error("Could not start deletion.");
      setSession(null); setAccount(null); window.history.replaceState({}, "", "/");
      setError("Account closed. Your library is queued for permanent deletion.");
    } catch { setError("Could not start deletion. Check your connection and try again."); }
    finally { setBusy(false); }
  }
  if (local) return <><SiteHeader /><div id="main-content" tabIndex={-1} className="app-content">{children}</div><SiteFooter /></>;
  if (loading) return <><SiteHeader /><main id="main-content" className="auth-screen"><p role="status">Opening your library…</p></main></>;
  if (!account) return <LandingPage error={error} />;
  return <><SiteHeader>{!account.local && <details className="account-menu" onKeyDown={event => { if (event.key === "Escape") { event.currentTarget.open = false; event.currentTarget.querySelector("summary")?.focus(); } }}>
    <summary><span className="account-avatar" aria-hidden="true">{account.email.slice(0, 1).toUpperCase()}</span>Account <span aria-hidden="true">⌄</span></summary>
    <div className="account-menu-panel"><p>Signed in as<strong>{account.email}</strong></p>
      <button disabled={busy} onClick={() => void logout()}>Sign out</button>
      <button disabled={busy} onClick={() => void logout(true)}>Sign out on all devices</button>
      <details className="account-danger"><summary>Delete account…</summary><p>Permanently remove your library and saved keys.</p><button className="btn-danger" disabled={busy} onClick={() => void deleteAccount()}>Delete account</button></details>
    </div>
  </details>}</SiteHeader>{error && <p className="page-notice" role="alert">{error}</p>}<div id="main-content" tabIndex={-1} className="app-content">{children}</div><SiteFooter /></>;
}
