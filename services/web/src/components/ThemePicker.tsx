import { useState } from "react";
import { Monitor, Moon, Sun, Sunset } from "lucide-react";
import { saveTheme, storedTheme, THEMES, type ThemeChoice } from "../theme";

const LABELS: Record<ThemeChoice, string> = {
  system: "Match system",
  light: "Light",
  warm: "Warm",
  dark: "Dark",
};

/**
 * Three light levels plus "match system". Small and unlabelled by design: it sits in the
 * book nav, where a heading above one select would be more chrome than the control.
 */
export function ThemePicker() {
  const [choice, setChoice] = useState<ThemeChoice>(storedTheme);
  const Icon = { system: Monitor, light: Sun, warm: Sunset, dark: Moon }[choice];
  return (
    <label className="theme-picker">
      <span className="visually-hidden">Theme</span>
      <Icon size={16} aria-hidden="true" />
      <select value={choice} onChange={(event) => {
        const next = event.target.value as ThemeChoice;
        setChoice(next);
        saveTheme(next);
      }}>
        {(["system", ...THEMES] as ThemeChoice[]).map((value) => (
          <option key={value} value={value}>{LABELS[value]}</option>
        ))}
      </select>
    </label>
  );
}
