const defaultTheme = require("tailwindcss/defaultTheme");
const colors = require("tailwindcss/colors");

module.exports = {
  content: [
    "./templates/**/*.html",
    "./core/**/*.py",
    "./building_mgmt/**/*.py",
  ],
  safelist: [
    "alert--success",
    "alert--warning",
    "alert--info",
    "alert--danger",
    "alert--error",
    // Notification badge/card colors added dynamically from Python,
    // so we safelist them to survive Tailwind's purge.
    "border-rose-200",
    "bg-rose-50",
    "text-rose-900",
    "dark:border-rose-800",
    "dark:bg-rose-900/40",
    "dark:text-rose-100",
    "bg-rose-100",
    "text-rose-700",
    "dark:bg-rose-900/60",
    "dark:text-rose-200",
    "border-amber-200",
    "bg-amber-100",
    "text-amber-900",
    "dark:border-amber-700",
    "dark:bg-amber-900/40",
    "dark:text-amber-100",
    "text-amber-700",
    "dark:bg-amber-900/60",
    "dark:text-amber-200",
    "border-emerald-200",
    "bg-emerald-100",
    "text-emerald-900",
    "dark:border-emerald-700",
    "dark:bg-emerald-900/30",
    "dark:text-emerald-100",
    "text-emerald-700",
    "dark:bg-emerald-900/60",
    "dark:text-emerald-200",
  ],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        brand: {
          DEFAULT: "#0f172a", // slate-900
          foreground: "#0f172a",
          accent: "#059669",
          surface: "#ffffff",
          muted: colors.slate[500],
          border: colors.slate[200],
        },
      },
      fontFamily: {
        sans: ["Inter", ...defaultTheme.fontFamily.sans],
      },
      boxShadow: {
        card: "0 10px 30px -12px rgba(15, 23, 42, 0.25)",
      },
    },
  },
  plugins: [
    require("@tailwindcss/forms"),
    require("@tailwindcss/typography"),
  ],
};
