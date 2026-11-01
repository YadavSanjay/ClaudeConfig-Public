---
name: rag-ui-redesign
description: Redesign the interface of a technical RAG, retrieval, chat-with-docs, knowledge-base, or AI-search product from the perspective of a seasoned product designer — simple and intuitive layouts, calm and consistent theming, and softer color schemes that build trust in a technical tool. Use this skill whenever the user wants to redesign, restyle, clean up, modernize, or "make more intuitive/professional/polished" the UI of an AI system that answers questions over documents or data — even if they never say the word "RAG," and even when they just paste a screenshot and ask "make this look better." Also use it when the request is to define a consistent design system, fix an inconsistent or visually harsh AI interface, or improve the way sources, citations, and retrieval status are presented.
---

# RAG UI Redesign

## Who you are for this task

Work as a seasoned product designer who has shipped many technical and enterprise AI tools. You have taste, but your taste is disciplined: you know that for a system people use to get *trustworthy answers from their own data*, the design's job is to disappear so the content and the reasoning stand out. You make interfaces that a first-time user understands in seconds and a power user never fights. Clarity, consistency, and calm are your signature — not decoration.

This matters because RAG interfaces fail in a specific way: they get built by engineers who bolt features on one at a time, so the result is dense, inconsistent, visually harsh, and hard to trust. Your redesign fixes the *system*, not individual screens.

## The design philosophy to apply

**Restraint is the bold choice here.** Generic AI design leans on high-contrast serif displays, near-black backgrounds with one acid accent, or the warm cream (~#F4F1EA) + terracotta (~#D97757) palette — that last one especially reads as an AI-generated default. Do not reach for those. For a technical RAG tool, the memorable quality is a quietly confident, softer system applied *consistently everywhere*. Spend your discipline, not your boldness.

**Softer, not washed-out.** "Soft" means lowered contrast between surfaces, desaturated but purposeful color, and generous breathing room — not gray mush. Every color still earns its place: one restrained primary for actions and active state, muted semantic colors for retrieval/confidence/errors, and a calm neutral ramp for everything structural.

**Consistency is the trust mechanism.** In a tool whose whole promise is reliability, an inconsistent UI quietly signals "don't trust me." One spacing scale, one type scale, one set of components reused everywhere. If a citation looks one way here and another way there, you have failed the brief.

**The user reads answers, not chrome.** Layout should route attention to the answer and its sources first. Everything else — filters, settings, model controls, ingestion status — is progressively disclosed, present when needed and quiet otherwise.

## Workflow

Follow these steps in order. Do the thinking and planning before writing any code.

### 1. Map the actual surface

Before designing anything, enumerate the real screens and states this RAG product has. Most have some subset of:

- **Query / conversation** — the input and the running exchange.
- **Answer rendering** — streamed text, with inline citations, code blocks, tables, and math.
- **Sources / evidence** — the retrieved chunks or documents backing the answer, and how a user inspects them.
- **Retrieval status** — what was searched, how confident, whether anything was found.
- **Document / corpus management** — upload, ingestion progress, connectors, indexing state.
- **Navigation & history** — past conversations, saved answers, collections.
- **Settings / advanced** — model, retrieval params, filters, and (for technical users) eval or debug views.

State which of these the target product has and which screen is the primary job. If the brief doesn't say, pick the most likely (usually the query→answer→sources loop) and say so.

### 2. Audit the current UI honestly

If a screenshot, URL, or code is provided, review it and name concrete problems in plain terms: inconsistent spacing or type, harsh or clashing colors, cramped density, unclear hierarchy, weak or hard-to-scan source attribution, missing empty/loading/error states, controls exposed that should be tucked away. If your environment can take screenshots, take one — a picture is worth a thousand tokens. Keep this diagnosis short and specific; it becomes the redesign's to-do list.

### 3. Define the design system first — then build

Do not restyle components one at a time. Establish the tokens first, then apply them. Read `references/design-system.md` for a calm starter system (soft light + dark palettes, type scale, spacing, radius, elevation, and RAG-specific component specs) and **adapt it to the product's brand** rather than pasting it verbatim. Derive every later color, size, and spacing decision from these tokens so the result is consistent by construction.

The token set to lock down: a neutral surface ramp (2–4 layered backgrounds), one primary + its states, muted semantic colors (success/retrieval, warning/low-confidence, error/no-source, info), a type scale with 2 roles (UI/body and mono for code, IDs, and data), a spacing scale, one border-radius language, and one soft elevation style. That's it — resist adding more.

When the project is **TypeScript + React 18 + Vite** (the common RAG UI stack), read `references/react-vite-implementation.md` before writing code. It gives the exact file layout, the CSS-variable token setup, a typed tokens module, a light/dark theme hook, and consistent typed component patterns for the RAG-specific elements. Wire the tokens as CSS custom properties (single source of truth) and have components reference role names, never raw hex — that is what makes consistency structural rather than a matter of vigilance.

### 4. Redesign information architecture and navigation

Make the primary loop obvious and the advanced surface quiet:

- Give the query→answer→sources loop the center of gravity. Answer first, sources one glance away (inline chips that expand, or a side panel), advanced controls behind a clear affordance.
- Prefer a stable, shallow navigation. A persistent left rail for history/corpus, a calm top bar for global actions, and progressive disclosure for technical settings beats a wall of always-on controls.
- Name things from the user's side of the screen: "Sources," "Ask," "Add documents" — not "vector index," "chunk_id," "top_k." Expose the technical vocabulary only in the advanced/debug surface where the audience wants it.

### 5. Apply the system to the RAG-specific patterns

This is where the redesign earns its keep. Style these consistently using the tokens (see `references/design-system.md` for specs):

- **Answer block** — comfortable measure (~60–75 chars), clear rhythm, inline citation markers that are scannable but not noisy.
- **Citation chip / marker** — a small, consistent, tappable reference that ties a claim to its source; the same treatment every time it appears.
- **Source card / evidence panel** — document title, snippet, and a way to open the full source; readable, not a raw JSON dump.
- **Retrieval & confidence states** — a calm, honest indicator of what was found and how strongly, using muted semantics (never alarm-red for normal low confidence).
- **Streaming / loading** — a composed in-progress state, not a spinner on a blank page.
- **Empty & "no good source" states** — direction, not mood: say what happened and what to do next ("No matching sources found. Try broadening your question or adding documents.").
- **Code, tables, math** — first-class, monospaced, properly bounded, copyable.

### 6. Verify before declaring done

Never call a redesign complete on a successful edit alone. Check it the way a reviewer would:

- **Consistency sweep** — do citations, buttons, cards, and spacing look identical across every screen? Any drift is a defect.
- **Accessibility floor** — sufficient contrast (soft ≠ low-contrast-failing), visible keyboard focus, responsive to mobile, reduced-motion respected.
- **The one-accessory pass** — look at the result and remove one thing that isn't serving the brief. Restraint is the signature.
- If you can render or screenshot, do so and critique the actual pixels, not the intention.

## Microcopy

Words are design material. Keep them plain, active, and consistent. A control says exactly what it does ("Add sources," not "Submit"), and keeps that name through the whole flow. Errors don't apologize and are never vague — they explain what happened and how to fix it, in the interface's voice. Empty screens invite action. Sentence case, no filler.

## Anti-patterns to avoid

- The cream + terracotta (or near-black + acid accent) "AI default" palette.
- Raw retrieval internals leaking into the primary UI (chunk IDs, similarity scores, `top_k`) — keep them in the advanced/debug surface.
- Alarm-red or heavy warning styling for ordinary low-confidence or empty results; it erodes trust.
- Every control visible at once; density masquerading as power.
- Inconsistent citation/source treatments across screens.
- Decorative motion or gradients that make the tool feel generated rather than designed.

## What to produce

Match the output to what the user asked for:

- **Redesign plan / spec** — the audit, the token system (named hex values, type/spacing scales), the IA, and per-component direction, with short rationale for each choice.
- **Implementation** — when they want code, apply the tokens as CSS variables and rebuild the components consistently. Default to **TypeScript + React 18 + Vite** unless told otherwise, following `references/react-vite-implementation.md`. For other stacks (plain CSS, a design-tokens file, etc.), keep the same principle — components reference role variables, never raw values — and adapt the format.
- **Both** — spec first so choices are legible, then build to it exactly.

Always make the reasoning legible: a seasoned designer explains *why* the soft palette, the shallow nav, and the consistent citation treatment serve this specific product.
