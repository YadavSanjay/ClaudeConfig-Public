# Implementation — TypeScript + React 18 + Vite

How to apply the design system in a React 18 + Vite + TS project so the result is consistent by construction. The rule underneath everything: **CSS custom properties are the single source of truth; components reference role names, never raw hex.** Use the values from `references/design-system.md`.

Contents:
1. File layout
2. Tokens as CSS variables (`tokens.css`)
3. Wire it up (`main.tsx`)
4. Typed token mirror (`tokens.ts`) — optional, for JS access
5. Light/dark theme hook + toggle
6. Component patterns (CitationChip, SourceCard, ConfidenceBadge)
7. If the project uses Tailwind
8. Consistency rules

---

## 1. File layout

```
src/
├── styles/
│   ├── tokens.css        # all design tokens as CSS variables (source of truth)
│   └── globals.css       # resets, base element styles, font imports
├── theme/
│   ├── tokens.ts         # typed semantic names (only if you need tokens in JS)
│   └── useTheme.ts       # light/dark hook
├── components/
│   ├── CitationChip/
│   │   ├── CitationChip.tsx
│   │   └── CitationChip.module.css
│   ├── SourceCard/
│   ├── ConfidenceBadge/
│   └── ThemeToggle.tsx
└── main.tsx
```

CSS Modules (`*.module.css`) work in Vite with no config and keep component styles scoped while still referencing the global variables.

## 2. Tokens as CSS variables (`src/styles/tokens.css`)

Define roles on `:root`, override the ramp under `[data-theme="dark"]`. (Values from `design-system.md`; trim or extend to your brand.)

```css
:root {
  /* surfaces & text */
  --surface-0: #F7F8FA;
  --surface-1: #FFFFFF;
  --surface-2: #EEF1F5;
  --border: #E2E6EC;
  --border-strong: #CDD3DC;
  --text-primary: #1E2530;
  --text-secondary: #5A6472;
  --text-muted: #8A93A3;

  /* brand + semantics */
  --primary: #4E63AE;
  --primary-hover: #43569A;
  --primary-tint: #EAEDF7;
  --success: #4F9D7B;
  --warning: #C9A24B;
  --danger: #C56B6B;
  --info: #5B8DBE;

  /* type */
  --font-ui: "Inter", system-ui, -apple-system, "Segoe UI", sans-serif;
  --font-mono: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace;

  /* spacing / radius / elevation */
  --space-1: 4px;  --space-2: 8px;  --space-3: 12px;
  --space-4: 16px; --space-5: 24px; --space-6: 32px; --space-8: 48px;
  --radius-sm: 6px; --radius-md: 10px; --radius-lg: 14px; --radius-pill: 999px;
  --shadow-sm: 0 1px 2px rgba(20,23,28,0.06);
  --shadow-md: 0 4px 12px rgba(20,23,28,0.08);
}

[data-theme="dark"] {
  --surface-0: #14171C;
  --surface-1: #1B1F26;
  --surface-2: #232833;
  --border: #2E3542;
  --border-strong: #3B4352;
  --text-primary: #E6E9EF;
  --text-secondary: #A6AEBC;
  --text-muted: #727B8A;

  --primary: #8695D6;
  --primary-hover: #97A4DE;
  --primary-tint: #22283A;
  --success: #6FB694;
  --warning: #D8BC72;
  --danger: #D08C8C;
  --info: #7FA9D4;

  --shadow-sm: 0 1px 2px rgba(0,0,0,0.30);
  --shadow-md: 0 4px 14px rgba(0,0,0,0.38);
}
```

`src/styles/globals.css` handles base styles so individual components don't re-declare them:

```css
* { box-sizing: border-box; }
html, body, #root { height: 100%; }
body {
  margin: 0;
  font-family: var(--font-ui);
  font-size: 15px;
  line-height: 1.6;
  color: var(--text-primary);
  background: var(--surface-0);
  -webkit-font-smoothing: antialiased;
}
@media (prefers-reduced-motion: reduce) {
  * { animation: none !important; transition: none !important; }
}
```

## 3. Wire it up (`src/main.tsx`)

```tsx
import React from "react";
import ReactDOM from "react-dom/client";
import "./styles/tokens.css";   // tokens first
import "./styles/globals.css";  // then base styles
import App from "./App";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
```

## 4. Typed token mirror (`src/theme/tokens.ts`) — optional

Only needed when a value must be read in JS (e.g. a chart color, an inline style). Keeps names identical to the CSS so there's still one vocabulary.

```ts
export const token = {
  surface0: "var(--surface-0)",
  surface1: "var(--surface-1)",
  surface2: "var(--surface-2)",
  border: "var(--border)",
  textPrimary: "var(--text-primary)",
  textSecondary: "var(--text-secondary)",
  textMuted: "var(--text-muted)",
  primary: "var(--primary)",
  success: "var(--success)",
  warning: "var(--warning)",
  danger: "var(--danger)",
  info: "var(--info)",
} as const;

export type TokenName = keyof typeof token;
```

Prefer plain CSS/CSS-Modules for styling; reach for this only where JS genuinely needs the value.

## 5. Light/dark theme hook + toggle (`src/theme/useTheme.ts`)

Respects the OS preference, persists the user's choice, and sets `data-theme` on `<html>` so every variable flips at once. (This runs in the user's own app, so `localStorage` is fine here.)

```ts
import { useEffect, useState, useCallback } from "react";

type Theme = "light" | "dark";
const KEY = "ui-theme";

function getInitial(): Theme {
  const saved = localStorage.getItem(KEY) as Theme | null;
  if (saved === "light" || saved === "dark") return saved;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function useTheme() {
  const [theme, setTheme] = useState<Theme>(getInitial);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem(KEY, theme);
  }, [theme]);

  const toggle = useCallback(
    () => setTheme((t) => (t === "light" ? "dark" : "light")),
    []
  );

  return { theme, setTheme, toggle };
}
```

```tsx
// src/components/ThemeToggle.tsx
import { useTheme } from "../theme/useTheme";

export function ThemeToggle() {
  const { theme, toggle } = useTheme();
  return (
    <button type="button" onClick={toggle} aria-label="Toggle color theme">
      {theme === "light" ? "Dark" : "Light"} mode
    </button>
  );
}
```

## 6. Component patterns

Each component styles only with variables and exposes a small typed prop surface. One full example, then the shape of the others.

**CitationChip** — the same treatment every time a claim references a source.

```tsx
// src/components/CitationChip/CitationChip.tsx
import styles from "./CitationChip.module.css";

interface CitationChipProps {
  index: number;              // e.g. 1, 2, 3
  sourceTitle: string;        // for the tooltip / aria-label
  onOpen?: (index: number) => void;
}

export function CitationChip({ index, sourceTitle, onOpen }: CitationChipProps) {
  return (
    <button
      type="button"
      className={styles.chip}
      title={sourceTitle}
      aria-label={`Source ${index}: ${sourceTitle}`}
      onClick={() => onOpen?.(index)}
    >
      {index}
    </button>
  );
}
```

```css
/* src/components/CitationChip/CitationChip.module.css */
.chip {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  margin: 0 2px;
  font: 500 12px/1 var(--font-ui);
  color: var(--primary);
  background: var(--primary-tint);
  border: none;
  border-radius: var(--radius-sm);
  cursor: pointer;
  vertical-align: baseline;
  transition: background 120ms ease;
}
.chip:hover { background: var(--primary); color: var(--surface-1); }
.chip:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
```

**SourceCard** — title → clamped snippet → muted meta row → open affordance.

```tsx
interface SourceCardProps {
  title: string;
  snippet: string;
  docName: string;
  location?: string;          // page / section
  updatedAt?: string;
  onOpen?: () => void;
}
// surface-1 background, --border, --radius-md, --space-4 padding.
// snippet: body-sm, --text-secondary, clamp to ~3 lines (-webkit-line-clamp: 3).
// meta row: 12px, --text-muted, mono for docName if it's an ID.
```

**ConfidenceBadge** — calm, honest retrieval status; muted semantics only.

```tsx
type Confidence = "high" | "limited" | "none";

interface ConfidenceBadgeProps {
  level: Confidence;
  sourceCount: number;
}
// high    -> --success  "Answered from N sources"
// limited -> --warning  "Limited sources — verify before relying on this"
// none    -> --danger (soft) "No matching sources found"
// Never fire-engine red; never alarm styling for ordinary low confidence.
```

## 7. If the project uses Tailwind

Vite + Tailwind is fine; map the same roles so class names stay semantic. In `tailwind.config.ts`:

```ts
export default {
  theme: {
    extend: {
      colors: {
        surface: { 0: "var(--surface-0)", 1: "var(--surface-1)", 2: "var(--surface-2)" },
        border: "var(--border)",
        primary: { DEFAULT: "var(--primary)", hover: "var(--primary-hover)", tint: "var(--primary-tint)" },
        success: "var(--success)", warning: "var(--warning)", danger: "var(--danger)", info: "var(--info)",
        text: { primary: "var(--text-primary)", secondary: "var(--text-secondary)", muted: "var(--text-muted)" },
      },
      borderRadius: { sm: "var(--radius-sm)", md: "var(--radius-md)", lg: "var(--radius-lg)" },
      fontFamily: { ui: "var(--font-ui)", mono: "var(--font-mono)" },
    },
  },
};
```

Then write `bg-surface-1 text-text-primary border-border rounded-md` — still one vocabulary, still driven by the CSS variables, so the theme toggle keeps working.

## 8. Consistency rules

- Never hard-code a hex, font, radius, or spacing value in a component — reference a variable. A raw color in a `.tsx` or module is a defect.
- One CitationChip, one SourceCard, one ConfidenceBadge, reused everywhere. If you're tempted to fork a variant, change the shared component with a prop instead.
- Keep base element styling in `globals.css`; components style only what's specific to them.
- Type every component's props; no `any`. The prop surface is part of the design contract.
- Accessibility floor is non-negotiable: `focus-visible` outlines, `aria-label`s on icon-only controls, contrast that holds in both themes, reduced-motion respected.
