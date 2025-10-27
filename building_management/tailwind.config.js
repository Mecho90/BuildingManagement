const defaultTheme = require("tailwindcss/defaultTheme");
const colors = require("tailwindcss/colors");

module.exports = {
  content: [
    "./templates/**/*.html",
    "./core/**/*.py",
    "./building_mgmt/**/*.py",
  ],
  safelist: ["alert--success", "alert--warning", "alert--info", "alert--danger", "alert--error"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        brand: {
          DEFAULT: "#0f172a", // slate-900
          foreground: "#0f172a",
          accent: "#0ea5e9",
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
