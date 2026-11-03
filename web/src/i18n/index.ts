import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import en from "./en";
import fa from "./fa";

export const LANG_STORAGE_KEY = "tla_lang";
export const SUPPORTED_LANGS = ["fa", "en"] as const;
export type AppLang = (typeof SUPPORTED_LANGS)[number];

function detectInitialLang(): AppLang {
  try {
    const stored = localStorage.getItem(LANG_STORAGE_KEY);
    if (stored === "fa" || stored === "en") return stored;
  } catch {
    // localStorage unavailable (private mode) — fall through.
  }
  const nav = typeof navigator !== "undefined" ? navigator.language.toLowerCase() : "";
  return nav.startsWith("fa") ? "fa" : "fa"; // Persian is the default fallback
}

export function applyLang(lang: AppLang): void {
  const dir = lang === "fa" ? "rtl" : "ltr";
  document.documentElement.setAttribute("lang", lang);
  document.documentElement.setAttribute("dir", dir);
}

export function setLang(lang: AppLang): void {
  try {
    localStorage.setItem(LANG_STORAGE_KEY, lang);
  } catch {
    // Non-fatal.
  }
  void i18next.changeLanguage(lang);
  applyLang(lang);
}

void i18next.use(initReactI18next).init({
  resources: {
    en: { translation: en },
    fa: { translation: fa },
  },
  lng: detectInitialLang(),
  fallbackLng: "fa",
  interpolation: { escapeValue: false },
  returnNull: false,
});

applyLang(i18next.language === "en" ? "en" : "fa");
