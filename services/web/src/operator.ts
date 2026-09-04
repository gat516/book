// The operator credential for knowledge-repair controls.
//
// Deliberately NOT the same thing as readerId. A reader id is a fake principal — a random
// per-browser string that names who is reading so progress can be stored against it; it
// authorizes nothing. Repair controls quarantine a book's facts and activate replacements,
// so they need something a reader cannot simply assert by typing a different id.
//
// This is a shared secret checked by reader-api (Config.RepairOperatorToken), not a user
// system: there is no per-operator identity and no rotation. It is the honest floor for a
// system whose only other credential, INGEST_INTERNAL_TOKEN, has total authority over the
// database — including novel deletion — and must never reach a browser.
//
// sessionStorage rather than localStorage on purpose: operator authority should end with
// the tab, while a reader id is meant to survive forever.
const KEY = "repair-operator-token";

export function operatorToken(): string {
  try {
    return sessionStorage.getItem(KEY) ?? "";
  } catch {
    // Private-mode browsers can throw on sessionStorage access. No token is the safe
    // answer: the reader keeps reading, the controls stay hidden.
    return "";
  }
}

export function setOperatorToken(token: string): void {
  try {
    if (token) {
      sessionStorage.setItem(KEY, token);
    } else {
      sessionStorage.removeItem(KEY);
    }
  } catch {
    // Nothing to do: the caller learns it failed from the next status response, whose
    // `operator` flag is the server's answer rather than this module's.
  }
}

export function clearOperatorToken(): void {
  setOperatorToken("");
}
