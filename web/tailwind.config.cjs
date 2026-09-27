/**
 * Tailwind configuration for the PuppetNET console.
 *
 * The compiled output is committed at `web/vendor/tailwind.css`, so deploying to
 * Cloudflare Pages needs no build step — point the Pages project at `web/` and
 * it serves static files. Rebuild after editing markup:
 *
 *     npm run build:css
 *
 * CI (`ci.yml`, job `web`) rebuilds and fails if the committed stylesheet is
 * stale, which is the usual way a purge-based setup silently loses a class.
 *
 * Content scanning covers `index.html`, `app.js` (which builds DOM from template
 * literals) and `styles.css` (which uses `@apply`). Every class must appear as a
 * complete literal string in one of those files — anything constructed at
 * runtime (`text-${colour}-400`) is invisible to the scanner and must be listed
 * in `safelist` below.
 */

const path = require("node:path");

/**
 * Content paths are resolved against *this file*, not the working directory:
 * `npm run build:css` from the repo root and from `web/` must produce the same
 * stylesheet, and a relative glob is how a purge silently drops every class.
 */
const CONTENT = ["index.html", "app.js", "styles.css", "tailwind.input.css"].map((file) => path.join(__dirname, file));

/** @type {import('tailwindcss').Config} */
module.exports = {
  content: CONTENT,
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        // Surfaces: matte, never pure black — pure black blooms on OLED and
        // hides the hairline borders that separate the panels.
        void: "#05070d",
        void2: "#070a12",
        panel: "#0b101c",
        panel2: "#0e1524",
        panel3: "#121b2e",
        raised: "#16203a",
        edge: "#1b2740",
        // Signal colours, matching the custom properties in styles.css.
        signal: {
          cyan: "#22d3ee",
          emerald: "#10b981",
          purple: "#a855f7",
          amber: "#f59e0b",
          rose: "#f43f5e",
        },
      },
      fontFamily: {
        sans: ["Inter", "Segoe UI", "system-ui", "-apple-system", "Helvetica Neue", "Arial", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "SFMono-Regular", "SF Mono", "Menlo", "Consolas", "Liberation Mono", "monospace"],
      },
      fontSize: {
        "2xs": ["10px", "14px"],
        micro: ["9.5px", "13px"],
      },
      boxShadow: {
        "glow-cyan": "0 0 0 1px rgba(34,211,238,.35), 0 0 18px rgba(34,211,238,.22)",
        "glow-emerald": "0 0 0 1px rgba(16,185,129,.35), 0 0 18px rgba(16,185,129,.20)",
        "glow-purple": "0 0 0 1px rgba(168,85,247,.35), 0 0 18px rgba(168,85,247,.20)",
        "glow-soft": "0 0 12px rgba(34,211,238,.14)",
        panel: "0 8px 30px rgba(0,0,0,.55)",
        modal: "0 24px 70px rgba(0,0,0,.7)",
      },
      keyframes: {
        "pulse-slow": {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: ".55" },
        },
        sweep: {
          "0%": { transform: "translateX(-100%)" },
          "100%": { transform: "translateX(100%)" },
        },
        breathe: {
          "0%, 100%": { opacity: ".45", transform: "scale(1)" },
          "50%": { opacity: "1", transform: "scale(1.07)" },
        },
      },
      animation: {
        "pulse-slow": "pulse-slow 2.6s cubic-bezier(.22,.61,.36,1) infinite",
        sweep: "sweep 1.15s cubic-bezier(.22,.61,.36,1) infinite",
        breathe: "breathe 2.4s cubic-bezier(.22,.61,.36,1) infinite",
      },
      transitionTimingFunction: {
        console: "cubic-bezier(.22,.61,.36,1)",
        "console-out": "cubic-bezier(.16,1,.3,1)",
      },
      zIndex: {
        canvas: "6",
        hud: "8",
        drawer: "35",
        modal: "100",
        toast: "120",
      },
    },
  },
  /**
   * Classes that only exist at runtime. The console paints per-entity colours
   * from data (inline styles, not utilities), so this list stays short: state
   * colours used by toggles in generated markup.
   */
  safelist: [
    { pattern: /^(bg|text|border)-(signal-cyan|signal-emerald|signal-purple|signal-amber|signal-rose)$/ },
    { pattern: /^(col|row)-span-\d$/ },
  ],
  corePlugins: {
    // The console is dark-only; shipping the light-mode variants of every
    // utility would double a stylesheet nobody can reach.
    preflight: true,
  },
  plugins: [],
};
