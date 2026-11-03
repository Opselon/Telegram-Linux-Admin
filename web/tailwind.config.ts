import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        // Dark-first infrastructure palette
        ink: {
          950: "#0B0F14",
          900: "#0F141B",
          850: "#131A23",
          800: "#182230",
          700: "#223042",
          600: "#2E405A",
        },
        accent: {
          DEFAULT: "#3B82F6",
          soft: "rgba(59,130,246,0.12)",
        },
        ok: "#22C55E",
        warn: "#F59E0B",
        danger: "#EF4444",
      },
      fontFamily: {
        sans: [
          "Inter",
          "Vazirmatn",
          "-apple-system",
          "BlinkMacSystemFont",
          "Segoe UI",
          "Roboto",
          "Helvetica Neue",
          "Arial",
          "sans-serif",
        ],
        mono: [
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "Consolas",
          "Liberation Mono",
          "monospace",
        ],
      },
      spacing: {
        safe: "env(safe-area-inset-bottom)",
      },
    },
  },
  plugins: [],
} satisfies Config;
