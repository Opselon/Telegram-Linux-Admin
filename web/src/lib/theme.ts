const THEME_KEY = "tla_theme";

export type Theme = "dark" | "light";

export function getInitialTheme(): Theme {
  try {
    const stored = localStorage.getItem(THEME_KEY);
    if (stored === "dark" || stored === "light") return stored;
  } catch {
    // Ignore.
  }
  return "dark"; // dark is the default
}

export function applyTheme(theme: Theme): void {
  document.documentElement.classList.toggle("dark", theme === "dark");
  document.documentElement.classList.toggle("light", theme === "light");
}

export function setTheme(theme: Theme): void {
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch {
    // Ignore.
  }
  applyTheme(theme);
}

import { useCallback, useState } from "react";

/** Theme toggle with a class-on-<html> apply; dark is the default. */
export function useTheme(): { dark: boolean; toggle: () => void; set: (t: Theme) => void } {
  const [dark, setDark] = useState(getInitialTheme() === "dark");
  const set = useCallback((theme: Theme) => {
    setTheme(theme);
    setDark(theme === "dark");
  }, []);
  const toggle = useCallback(() => {
    set(dark ? "light" : "dark");
  }, [dark, set]);
  return { dark, toggle, set };
}
