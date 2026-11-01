# Starter Design System — Soft, Calm, Technical RAG

A calm, trustworthy starting system. Treat every value here as a sensible default to **adapt to the product's brand**, not a mandate. The point is consistency: pick a ramp and a primary, then derive everything from them.

Contents:
1. Color — light theme
2. Color — dark theme
3. Typography
4. Spacing, radius, elevation
5. RAG component specs
6. How to apply (CSS variables / theme)

---

## 1. Color — light theme

A cool, soft neutral ramp (deliberately **not** warm cream, which reads as an AI default) with one muted primary and muted semantics.

```
--surface-0:      #F7F8FA   /* app background */
--surface-1:      #FFFFFF   /* cards, panels, answer surface */
--surface-2:      #EEF1F5   /* subtle raised: user message, hover */
--border:         #E2E6EC   /* hairlines, dividers */
--border-strong:  #CDD3DC

--text-primary:   #1E2530
--text-secondary: #5A6472
--text-muted:     #8A93A3

--primary:        #4E63AE   /* muted indigo — actions, active state, links */
--primary-hover:  #43569A
--primary-tint:   #EAEDF7   /* active/selected background */

--success:        #4F9D7B   /* retrieval found / high confidence */
--warning:        #C9A24B   /* low confidence (muted amber, not alarm) */
--danger:         #C56B6B   /* no source / error (soft, never fire-engine red) */
--info:           #5B8DBE
```

Alternate primary if indigo doesn't fit the brand: a muted teal `--primary: #3E7C86` reads equally calm and technical. Swap the tint to match (`#E7F0F1`).

## 2. Color — dark theme

Keep the same roles; lift the primary and semantics for legibility on dark surfaces. Soft dark ≠ pure black.

```
--surface-0:      #14171C
--surface-1:      #1B1F26
--surface-2:      #232833
--border:         #2E3542
--border-strong:  #3B4352

--text-primary:   #E6E9EF
--text-secondary: #A6AEBC
--text-muted:     #727B8A

--primary:        #8695D6
--primary-hover:  #97A4DE
--primary-tint:   #22283A

--success:        #6FB694
--warning:        #D8BC72
--danger:         #D08C8C
--info:           #7FA9D4
```

## 3. Typography

Two roles only. A clean UI/body sans, and a mono for code, IDs, chunk references, and numeric data — mono signals "technical" without shouting.

```
--font-ui:   "Inter", system-ui, -apple-system, "Segoe UI", sans-serif;
--font-mono: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace;
```

Type scale (role → size / line-height / weight):

```
caption / meta      12px  / 16  / 500   (source meta, timestamps, labels)
body-sm             13px  / 20  / 400   (secondary text, chip labels)
body                15px  / 24  / 400   (answer text — comfortable reading)
body-strong         15px  / 24  / 600
subtitle            18px  / 26  / 600   (panel headers)
title               22px  / 30  / 600   (page / conversation title)
display             30px  / 38  / 700   (empty-state hero, sparingly)
```

Answer text measure: cap line length around 60–75 characters for readability. Body line-height stays generous — breathing room is most of what makes an interface feel "soft."

## 4. Spacing, radius, elevation

```
/* spacing — 4px base */
--space-1: 4px;  --space-2: 8px;  --space-3: 12px;
--space-4: 16px; --space-5: 24px; --space-6: 32px; --space-8: 48px;

/* radius — rounded but not bubbly */
--radius-sm: 6px;    /* chips, inputs, small controls */
--radius-md: 10px;   /* cards, buttons, message bubbles */
--radius-lg: 14px;   /* panels, modals */
--radius-pill: 999px;

/* elevation — soft, low-spread, single layer */
--shadow-sm: 0 1px 2px rgba(20,23,28,0.06);
--shadow-md: 0 4px 12px rgba(20,23,28,0.08);
/* prefer borders over heavy shadows for a calm, flat-ish feel */
```

## 5. RAG component specs

Apply these identically everywhere they appear — consistency is the brief.

**Message: user vs assistant**
- User message: `--surface-2` background, `--radius-md`, right-aligned or clearly offset. Compact.
- Assistant answer: sits on `--surface-1` with comfortable padding (`--space-5`), `body` type, clear vertical rhythm between paragraphs. No bubble needed — let the answer breathe as the primary content.

**Citation chip / inline marker**
- A small pill or superscript tied to a claim: `--radius-sm`, `body-sm`, `--primary` text on `--primary-tint`, with a source number or short title.
- Tappable/hoverable → reveals the source snippet in a popover or scrolls the evidence panel to it.
- Identical treatment every single time. Never render a raw `[doc_3:chunk_12]` in the primary surface.

**Source card (evidence panel)**
- `--surface-1`, `--border`, `--radius-md`, `--space-4` padding.
- Title (`body-strong`) → snippet (`body-sm`, `--text-secondary`, clamped to ~3 lines) → meta row (`caption`, `--text-muted`: doc name, page/section, updated date) → "Open source" affordance.
- Optional relevance indicator as a quiet bar or muted `--success` dot — not a loud percentage unless the audience is technical and it's in an advanced view.

**Retrieval / confidence status**
- One calm line or badge: e.g. "Answered from 4 sources" with a muted `--success` marker.
- Low confidence → `--warning`, phrased honestly and helpfully ("Limited sources — verify before relying on this"). Never `--danger` for normal low confidence.

**Streaming / loading**
- Composed in-progress state: a subtle shimmer on the forming answer or a calm "Searching your documents…" line with the retrieval step named. Avoid a bare spinner on a blank page.

**Empty & no-source states**
- Direction, not mood. Display type + one plain sentence + a clear next action. Example: "No matching sources found. Try broadening your question or adding documents." with an "Add documents" button.

**Input composer**
- `--surface-1`, `--border`, `--radius-md`, generous padding; primary "Ask" action in `--primary`. Advanced controls (filters, source scope, model) tucked behind a single quiet affordance, not spread across the bar.

**Code / tables / math**
- `--font-mono`, own bounded surface (`--surface-2` or a slightly inset panel), horizontal scroll contained, copy affordance. Tables get zebra rows using `--surface-2` at low opacity and a hairline `--border`.

## 6. How to apply

Define the tokens once as CSS custom properties on `:root` (and a `[data-theme="dark"]` override), then reference the variables everywhere — never hard-code a hex in a component. In Tailwind, map these to `theme.extend.colors` / `spacing` / `borderRadius` and use the semantic names (`bg-surface-1`, `text-primary`, `border-border`). In a design-tokens file, export the same names. The rule that keeps it consistent: components reference roles (`--primary`, `--surface-1`), never raw values.

For a **TypeScript + React 18 + Vite** project, see `references/react-vite-implementation.md` — it turns this system into the concrete file layout, a `tokens.css`, a typed theme hook, and consistent typed components for the RAG patterns above.
