import { useState } from "react";
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
  return (
    <label className="theme-picker">
      <span className="visually-hidden">Theme</span>
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
