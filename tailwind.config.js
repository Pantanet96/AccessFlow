/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./app/templates/**/*.html"],
  // Tailwind's content scanner only sees literal class-name substrings in the
  // scanned files. Several of our own component classes are either not yet
  // consumed by any template, or are assembled from a Jinja variable at
  // render time (e.g. class="days days--{{ tone }}" in index.html), so the
  // scanner can never "see" the full literal string. Safelist them so the
  // hand-authored component library in src/input.css always ships in full,
  // regardless of which templates currently reference which class.
  safelist: [
    "active",
    "glass-panel", "cinematic-shadow", "custom-scrollbar",
    "sidebar-link", "bottomnav-link",
    "badge", "badge--active", "badge--expired", "badge--suspended", "badge--cancelled",
    "btn-sm", "btn-red", "btn-brand",
    "row-actions", "responsive",
    "audit-feed", "audit-item", "af-main", "af-target", "af-detail", "af-when", "af-body",
    "lib-sum", "inline-validate",
    "sub-card", "sub-card__head", "sub-card__body", "sub-card__foot",
    "plan-name", "plan-price", "expiry", "days", "days--ok", "days--warn", "days--late",
    "manager", "pending", "renew-box",
    "suspend-banner", "pay-history",
    "launch", "launch-tile", "launch-tile__txt", "launch-tile--brand", "launch-tile--seer",
    "users-toolbar", "users-count", "contact-cell", "account-handle",
    "logout-form", "logout-btn", "app-footer",
    "toggle-switch", "toggle-switch__input", "toggle-switch__track", "toggle-switch__thumb",
    "settings-card", "form-label", "form-input", "btn-primary",
  ],
  theme: {
    extend: {
      colors: {
        // Each token is backed by a CSS variable (R G B, base 10) set per
        // [data-theme="..."] in src/input.css — NOT a flat hex — so Tailwind's
        // opacity modifiers (bg-status-active/20, bg-surface-container-low/80,
        // etc.) keep working across all 3 selectable themes. Do not replace
        // these with plain hex strings.
        background: "rgb(var(--color-background) / <alpha-value>)",
        surface: "rgb(var(--color-surface) / <alpha-value>)",
        "surface-dim": "rgb(var(--color-surface-dim) / <alpha-value>)",
        "surface-container-lowest": "rgb(var(--color-surface-container-lowest) / <alpha-value>)",
        "surface-container-low": "rgb(var(--color-surface-container-low) / <alpha-value>)",
        "surface-container": "rgb(var(--color-surface-container) / <alpha-value>)",
        "surface-container-high": "rgb(var(--color-surface-container-high) / <alpha-value>)",
        "surface-container-highest": "rgb(var(--color-surface-container-highest) / <alpha-value>)",
        "surface-elevated": "rgb(var(--color-surface-elevated) / <alpha-value>)",
        "on-surface": "rgb(var(--color-on-surface) / <alpha-value>)",
        "on-surface-variant": "rgb(var(--color-on-surface-variant) / <alpha-value>)",
        outline: "rgb(var(--color-outline) / <alpha-value>)",
        "outline-variant": "rgb(var(--color-outline-variant) / <alpha-value>)",
        primary: "rgb(var(--color-primary) / <alpha-value>)",
        "primary-container": "rgb(var(--color-primary-container) / <alpha-value>)",
        "on-primary": "rgb(var(--color-on-primary) / <alpha-value>)",
        secondary: "rgb(var(--color-secondary) / <alpha-value>)",
        "on-secondary": "rgb(var(--color-on-secondary) / <alpha-value>)",
        tertiary: "rgb(var(--color-tertiary) / <alpha-value>)",
        "on-tertiary": "rgb(var(--color-on-tertiary) / <alpha-value>)",
        "status-active": "rgb(var(--color-status-active) / <alpha-value>)",
        "status-suspended": "rgb(var(--color-status-suspended) / <alpha-value>)",
        "status-expired": "rgb(var(--color-status-expired) / <alpha-value>)",
        "text-muted": "rgb(var(--color-text-muted) / <alpha-value>)",
        error: "rgb(var(--color-error) / <alpha-value>)",
      },
      borderRadius: {
        DEFAULT: ".25rem",
        lg: ".5rem",
        xl: ".75rem",
        full: "9999px",
      },
      spacing: {
        "gap-xs": "4px",
        "gap-sm": "8px",
        "gap-md": "16px",
        "gap-lg": "24px",
      },
      maxWidth: {
        app: "1440px",
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "-apple-system", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
    },
  },
  plugins: [],
};
