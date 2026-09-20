export const THEMES = ["light", "warm", "dark"] as const;
export type Theme = typeof THEMES[number];
/** No stored choice: the system's own light/dark preference decides (index.css). */
export type ThemeChoice = Theme | "system";

const KEY = "novel-engine:theme";

export function storedTheme(): ThemeChoice {
  try {
    const value = localStorage.getItem(KEY);
    return THEMES.includes(value as Theme) ? (value as Theme) : "system";
  } catch {
    return "system"; // private mode or blocked storage: follow the system
  }
}

/**
 * Put the choice on <html> so every rule in the sheet resolves against it. Removing the
 * attribute is what hands the decision back to prefers-color-scheme, rather than pinning
 * whichever mode the system happened to be in when the page loaded.
 */
export function applyTheme(choice: ThemeChoice): void {
  const root = document.documentElement;
  if (choice === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", choice);
}

export function saveTheme(choice: ThemeChoice): void {
  applyTheme(choice);
  try {
    if (choice === "system") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, choice);
  } catch { /* the theme still applies for this session */ }
}
