/** @type {import('tailwindcss').Config} */
module.exports = {
  // Files Tailwind scans for class names.
  // Root templates, per-app templates, static JS (for class toggling in JS),
  // and app Python (for class strings in widget attrs).
  content: [
    "./templates/**/*.html",
    "./*/templates/**/*.html",
    "./static/**/*.js",
    // Only include .py if you actually put class strings in Python
    // (e.g. widgets = {"attrs": {"class": "..."}}). Otherwise delete this line —
    // it slows the scan and can pull in false positives from docstrings.
    // "./*/**/*.py",
  ],

  // Classes added or toggled by JavaScript at runtime cannot be detected by
  // the content scanner, so they must be listed explicitly here.
  safelist: [
    "hidden",
    "flex",
    "flex-col",
    "md:flex",
    "md:flex-row",
    "md:items-center",
    "md:ml-auto",
    "md:w-auto",
    "pt-4",
    "md:pt-0",
    "w-full",
  ],

  darkMode: "class",

  theme: {
    extend: {
      colors: {
        eventgate: {
          primary: "#064e3b",
          accent: "#34d399",
        },
      },
    },
  },

  plugins: [],
};