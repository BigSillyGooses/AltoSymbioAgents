/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./desktop-ui/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      fontFamily: {
        sans: [
          "-apple-system",
          "BlinkMacSystemFont",
          "Segoe UI",
          "Helvetica Neue",
          "sans-serif",
        ],
        mono: [
          "SF Mono",
          "Cascadia Code",
          "JetBrains Mono",
          "Menlo",
          "Consolas",
          "monospace",
        ],
      },
      colors: {
        // Warm light scale — page is cream, cards float as white-on-cream
        bg: {
          DEFAULT: "#fbf9f5",
          1: "#ffffff",
          2: "#f4f1ea",
          3: "#ebe6db",
          4: "#dcd5c5",
        },
        line: {
          DEFAULT: "#e8e2d4",
          soft: "#f1ece0",
        },
        ink: {
          DEFAULT: "#2a2730",
          dim: "#6b6577",
          faint: "#a09aab",
        },
        // Pastel accents — desaturated, never candy-bright
        accent: {
          DEFAULT: "#b4a7f5",
          dark: "#9485e6",
        },
        // Status tokens are tuned for TEXT readability on cream
        // (~5:1 contrast). When used as fills (bg-warn/20, bg-err/10
        // etc.) the low opacity produces gentle pastel washes — so one
        // value covers both roles without screaming.
        ok: "#4a8a68",
        warn: "#b87a2e",
        err: "#b85547",
        claude: "#c97a64",
        local: "#5a9a82",
      },
      boxShadow: {
        // Plum-tinted shadow stacks — never pure black, always layered
        "soft-1":
          "0 1px 2px rgba(60,40,80,0.04), 0 2px 8px rgba(60,40,80,0.04)",
        "soft-2":
          "0 2px 4px rgba(60,40,80,0.05), 0 8px 24px rgba(60,40,80,0.06)",
        "soft-3":
          "0 4px 8px rgba(60,40,80,0.06), 0 16px 48px rgba(60,40,80,0.08)",
        "soft-inset": "inset 0 1px 2px rgba(60,40,80,0.06)",
        "accent-glow":
          "0 4px 16px rgba(180,167,245,0.35), 0 2px 4px rgba(180,167,245,0.2)",
        glass:
          "0 4px 24px rgba(60,40,80,0.08), 0 8px 40px rgba(60,40,80,0.06)",
      },
    },
  },
  plugins: [],
};
