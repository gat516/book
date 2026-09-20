import { readerId } from "../readerId";
import { useEffect, useState, type ReactNode } from "react";
import { setSession, sessionHeaders, type Session, canUseLegacyLocalSession, legacyLocalSession } from "../session";

export function AuthGate({ children }: { children: ReactNode }) {
  const [account, setAccount] = useState<Session | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    const expired = () => { setAccount(null); setError("Your session ended. Sign in again."); };
    window.addEventListener("book-session-expired", expired);
    fetch("/api/auth/session", { credentials: "same-origin" }).then(async response => {
      if (canUseLegacyLocalSession(import.meta.env.DEV, window.location.hostname, response.status)) return legacyLocalSession(readerId());
      if (response.status === 401) return null;
      if (!response.ok) throw new Error("Sign-in is temporarily unavailable.");
      return response.json() as Promise<Session>;
    }).then(session => { if (alive) { setSession(session); setAccount(session); } })
      .catch(e => { if (alive) setError(e instanceof Error ? e.message : "Could not connect."); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; window.removeEventListener("book-session-expired", expired); };
  }, []);
  async function logout(all = false) {
    const response = await fetch(`/api/auth/${all ? "revoke-sessions" : "logout"}`, { method: "POST", headers: sessionHeaders() });
    if (!response.ok) { setError("Could not sign out. Please retry."); return; }
    setSession(null); setAccount(null); window.history.replaceState({}, "", "/");
  }
  async function deleteAccount() {
    if (window.prompt("This permanently deletes your library and saved keys. Type DELETE to continue.") !== "DELETE") return;
    const response = await fetch("/api/auth/account", { method: "DELETE", headers: sessionHeaders() });
    if (!response.ok) { setError("Could not start deletion. Please retry."); return; }
    setSession(null); setAccount(null); setError("Account closed. Your library is queued for permanent deletion.");
  }
  if (loading) return <main className="auth-screen"><p>Loading your library…</p></main>;
  if (!account && error) return <main className="auth-screen"><h1>Could not open your library</h1>
    <p role="alert">{error}</p><button onClick={() => window.location.reload()}>Try again</button></main>;
  if (!account) {
    const invite = new URLSearchParams(window.location.search).get("invite");
    return <main className="auth-screen"><h1>Your private reading library</h1>
      <p>Translate novels and explore their wiki as you read. Your books and model API keys belong to your account.</p>
      <p>This site is invitation only. Sign in with the Google account your invitation was sent to.</p>
      {error && <p role="alert">{error}</p>}
      <a className="button" href={`/api/auth/login${invite ? `?invite=${encodeURIComponent(invite)}` : ""}`}>Sign in with Google</a>
    </main>;
  }
  return <>{!account.local && <div className="account-bar"><span>{account.email}</span>
    <button onClick={() => void logout()}>Sign out</button>
    <button onClick={() => void logout(true)}>Sign out everywhere</button>
    <button onClick={() => void deleteAccount()}>Delete account</button>
    {error && <span role="alert">{error}</span>}
  </div>}{children}</>;
}
