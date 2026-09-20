// An invited reader can explore the public sample and still finish signup.
// Keep the single-use invitation only in same-origin URLs, never browser storage.
export function publicHref(path: string, search = window.location.search): string {
  const invite = new URLSearchParams(search).get("invite");
  return invite ? `${path}?invite=${encodeURIComponent(invite)}` : path;
}
