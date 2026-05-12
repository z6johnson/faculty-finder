# Seed Style Principles — Agent Instructions

This document tells coding agents (Claude Code, Cursor, Copilot, and similar) how to make visual and interaction decisions when working in this repository. It is grounded in the International Typographic Style and translates that posture into rules an agent can follow without supervision on every commit.

These are principles, not a component library. Apply them to whatever stack and framework this repository uses. If a rule conflicts with a documented project token or an explicit instruction in the task, the project-specific value wins. Surface the conflict in the PR description so it gets recorded.

When in doubt, do less. Restraint is the default.

---

## 0. Accessibility Is Non-Negotiable

Read this first. It governs every other principle in this document.

All UI and UX produced in this repository must conform to **WCAG 2.1 Level AA at minimum**, with WCAG 2.2 AA as the working target. WCAG 2.1 AA is the operative legal standard for digital accessibility under the ADA (DOJ Title II rule, 2024) and is the institutional baseline for UC work. WCAG 2.2 adds requirements we will need anyway; build to it now rather than retrofit later.

ADA + WCAG conformance is the floor, not the ceiling. Inclusive design goes further than the checklist. When the checklist passes but a real user cannot complete the task, the work is not done.

**Binding rules:**
- No component, view, or feature ships without meeting WCAG 2.1 AA. If you cannot verify conformance, the work is not complete. Say so; do not ship and flag for later.
- Use semantic HTML or the platform's semantic equivalent first. ARIA fills gaps in semantics; it does not replace them.
- Every interactive element is reachable by keyboard, has a visible focus state, has an accessible name, and exposes its state to assistive technology.
- All text and meaningful UI elements meet contrast requirements: 4.5:1 for normal text, 3:1 for large text and UI components, against both light and dark backgrounds if both are supported.
- Color is never the sole carrier of meaning. Pair it with text, weight, position, or iconography.
- Respect `prefers-reduced-motion`, `prefers-color-scheme`, and `prefers-contrast` wherever the platform supports them.
- Touch targets meet WCAG 2.2 minimum size (24x24 CSS pixels) and are spaced to avoid accidental activation.
- Do not rely on hover-only interactions. Every hover-revealed action has a keyboard and touch equivalent.
- Forms have programmatically associated labels, clear error identification, and inline error recovery guidance.
- Media has captions, transcripts, or text alternatives as applicable. Decorative imagery is marked decorative; meaningful imagery has accurate `alt` text.

**Required in the test suite:**
- Automated accessibility tests (axe, Pa11y, Lighthouse, or the project's equivalent) run on every user-facing change. Failing tests block merge.
- Keyboard navigation tests for any new interactive component.
- Manual accessibility review documented in the PR for new patterns or substantive UX changes. Automated tools catch about a third of real issues; the rest requires human review.

**When you generate code:**
- Lead with semantic markup. If you reach for a `<div>` with click handlers, justify why a `<button>` or `<a>` will not work.
- Add accessibility annotations as you write the component, not afterward.
- Do not suppress or override accessibility warnings from linters or build tools without documenting why in the same commit.

The remaining principles assume this floor. Every visual and interaction rule below is constrained by it. If a stylistic preference in this document conflicts with WCAG conformance, accessibility wins.

---

## 1. Typography Carries the Hierarchy

Type is the primary design material. Size, weight, tracking, and case create structure. Colored boxes, icons, and containers do not.

**Rules:**
- Maintain a type scale with no more than five distinct sizes in active use on any single view. If a layout seems to need a sixth, the hierarchy is wrong, not the scale.
- Define the scale in tokens (`--font-size-display`, `--font-size-body`, etc., or the project equivalent). Do not hardcode `font-size` values in components.
- Use a "system" register for labels and metadata: small, bold, uppercase, wide-tracked. Use it consistently so it reads as system rather than content.
- Use a "reading" register for body prose: comfortable size, line-height between 1.5 and 1.7, normal tracking. Body text scales with user font-size preferences; do not lock it.
- Use a "display" register for headers and primary signal: larger, tighter tracking, confident weight.
- For projects with substantial prose alongside structural UI, set up two typeface families: sans-serif for command and organization, serif for reading and analysis. For dense tools and dashboards, one sans-serif family is correct.
- Heading levels follow document structure (h1 → h2 → h3). Do not skip levels for visual sizing; use type tokens to adjust visual weight while preserving semantics.

**When you generate code:**
- Centralize type tokens in one file. Reference them by name everywhere else.
- Use relative units (`rem`, `em`) for type sizing so user preferences are respected.
- Do not introduce a new size, weight, or family without adding it to the token file and noting why in the PR.

---

## 2. Color Is Semantic, Not Decorative

The working palette is achromatic. Color appears only when it carries meaning.

**Rules:**
- Build the interface on a grayscale spectrum with enough steps (typically 8 to 12) to create depth, hierarchy, and state without color.
- Reserve color for specific semantics: urgency, status, error, success, opportunity, active state. One accent color is usually sufficient. A second is permitted if muted enough not to compete.
- Every color pairing meets WCAG 2.1 AA contrast as specified in section 0. Verify with a contrast checker, not by eye.
- Color is never the sole differentiator for any state. Always pair color with weight, text, position, or iconography.
- Test the interface in grayscale and in colorblind-simulation modes. If meaning is lost, the design is incomplete.
- Do not introduce brand or marketing color into functional UI without a documented reason.

**When you generate code:**
- Define all colors as named tokens. Do not use raw hex values in components.
- When adding a new colored state, also specify the non-color signal that accompanies it. If you cannot, the state is not yet defined; stop and ask.

---

## 3. Earn Every Visual Element

Nothing exists for ornament. Every border, shadow, fill, and divider must serve comprehension or reduce friction.

**Rules:**
- Prefer whitespace for separation. Add a line only when spacing alone creates ambiguity.
- Use at most two border weights: hairline for subtle structure, heavy for emphasis. Keep radius minimal or zero.
- A left-edge accent border is a permitted pattern for signaling priority or category on cards and callouts. Use it sparingly.
- Shadows, gradients, and background fills appear only when they communicate something specific: elevation for a modal, a subtle wash to group related content. Never for visual interest.
- Do not add icons that duplicate adjacent text. An icon either replaces text or extends it; it does not decorate it. Decorative icons are marked `aria-hidden`; functional icons have accessible names.

**When you generate code:**
- Before adding a visual element, state in a comment or PR note what it communicates. If you cannot, do not add it.
- When porting components from a library, strip default ornamentation (gratuitous shadows, gradients, rounded corners) before integrating.

---

## 4. Respect the Grid

All spacing derives from a fixed base unit. No magic numbers.

**Rules:**
- Define a base unit (commonly 4px or 8px) and derive all padding, margin, gap, and structural dimensions from multiples of it.
- Centralize layout tokens: grid gap, page padding, header height, status bar height, sidebar width. Reference them by name.
- Layouts reflow gracefully at user-scaled text sizes up to 200% without loss of content or function (WCAG 1.4.4).
- Content remains usable at 400% zoom on a 1280px-wide viewport without requiring horizontal scrolling for primary reading flow (WCAG 1.4.10).

**When you generate code:**
- Use spacing tokens in every layout rule. Reject raw pixel values for spacing.
- Test responsive behavior at standard breakpoints and at user-zoomed sizes before merging.

---

## 5. Motion Confirms, It Does Not Perform

Transitions exist to confirm state changes. They do not entertain.

**Rules:**
- Keep durations short: 120 to 200ms for micro-interactions, up to 300ms for larger layout shifts. Anything longer needs a documented reason.
- Use simple easing curves. Standard ease-out or ease-in-out is correct in nearly all cases.
- Hover effects are barely perceptible: a color shift, a subtle border change. No scale transforms, no bounces.
- No entrance animations on page load. Content appears; it does not arrive.
- Honor `prefers-reduced-motion: reduce`. When set, disable non-essential transitions, parallax, auto-playing video, and any motion that loops or moves beyond a small viewport area (WCAG 2.3.3).
- No content flashes more than three times per second (WCAG 2.3.1). Period.

**When you generate code:**
- Define motion tokens (duration, easing) and reference them. Do not hardcode timing values.
- Wrap non-essential animations in a reduced-motion media query that turns them off, not just slows them down.

---

## 6. Controls Are Typography

Buttons, links, and form elements are extensions of the type system, not separate decorative objects.

**Rules:**
- Primary action: filled, terse label. Secondary action: outlined or borderless. Tertiary action: styled text with a hover state. Use this hierarchy consistently.
- Inputs for inline editing may use a bottom-border-only treatment that disappears into the content until activated. Dedicated forms use full borders.
- Focus states are always visible and high-contrast. Use a solid outline at least 2px thick with 3:1 contrast against adjacent colors (WCAG 2.4.11). Never remove the focus ring without replacing it with something equally legible.
- Touch targets are at least 24x24 CSS pixels (WCAG 2.2 minimum); 44x44 is preferred for primary actions.
- Style dropdowns and selects to match the surrounding type. When replacing native controls, the replacement exposes the same accessibility semantics (role, state, keyboard support) the native version provided.
- Use a `<button>` for actions and `<a>` for navigation. Do not mix them.

**When you generate code:**
- Build button, input, and link components against the type tokens, not against an isolated component spec.
- Test keyboard navigation on every interactive component before merging. If focus is not visible at every step, the component is not done.
- For any custom interactive component, reference the matching ARIA Authoring Practices pattern. Do not invent keyboard interactions.

---

## 7. Write for Scanners

UI copy follows the same discipline as the visuals.

**Rules:**
- Labels are terse, specific, and consistent in grammatical structure across the interface. Pick a pattern (verb-first for actions, noun-first for fields) and hold it.
- Link text describes the destination. Avoid "click here" or "read more" detached from context (WCAG 2.4.4).
- Timestamps are relative when recency matters ("2 minutes ago"), absolute when precision does ("Mar 14, 2026, 9:42 AM"). Decide per context, document the choice.
- Status messages use plain active language. Lead with the implication for the user, not the internal state of the system. Programmatic status updates use `aria-live` regions or equivalent so screen readers announce them (WCAG 4.1.3).
- Error messages explain what happened and what the user can do. No apologies, no jargon, no stack traces in user-facing copy. Errors are identified in text, not just by color (WCAG 1.4.1, 3.3.1).
- Empty states are quiet: a single line of subdued text, centered, naming what could be here and how to put it there.
- Set the page or view language attribute so assistive technology pronounces content correctly (WCAG 3.1.1).

**When you generate code:**
- Centralize UI strings where the framework supports it. Review them as carefully as the components that display them.
- Do not generate placeholder copy ("Lorem ipsum", "Sample text") in shipped views. If real copy is not available, leave the slot empty and flag it.

---

## 8. Fixed Information Hierarchy

Every view answers the same three questions in the same order.

**Rules:**
- Order content as: what needs attention right now, then the broader context, then the source and provenance.
- Apply this ordering at every scale: cards, detail views, notifications, dashboards.
- Signal and urgency surface first. Institutional or operational meaning comes second. Metadata sits at the bottom.
- Heading structure reflects the visual hierarchy. Reading order in the DOM matches reading order on screen (WCAG 1.3.2).

**When you generate code:**
- Before building a new view, write out the three answers for this specific surface. If you cannot answer them clearly, the view is not yet specified; stop and ask.

---

## 9. Ambient Over Interruptive

Persistent status belongs in the architecture. Modals are for deliberate decisions.

**Rules:**
- Glanceable status (connection health, sync state, counts, timestamps) lives in a persistent footer bar, header strip, or equivalent. Not in alerts.
- Toasts handle transient confirmations and dismiss themselves. They do not require action. Toasts containing important information persist long enough for users to read at their own pace, or remain dismissible (WCAG 2.2.1).
- Modals are reserved for actions that require deliberate confirmation or input the user must engage with before continuing. Focus is trapped within the modal while open, returns to the trigger on close, and the modal is dismissible by keyboard (Escape).
- Notifications and badges count only things the user can act on. Do not pad them with informational items to drive engagement.
- No content moves, blinks, scrolls, or auto-updates without a user-accessible mechanism to pause, stop, or hide it if it persists longer than five seconds (WCAG 2.2.2).

**When you generate code:**
- Build the ambient surface (footer, header strip) first when starting a new project. It is structural, not decorative.
- For modals, use the platform's accessible dialog primitive (`<dialog>`, the framework's modal component with focus management). Do not roll your own.

---

## Required Project Artifacts

Every project using these principles maintains, at minimum:

1. **A token file** defining the project's specific values for color, type, spacing, and motion. One source of truth, referenced everywhere.
2. **A short design notes document** recording any intentional deviations from these principles and why. Keep it brief; long deviation lists indicate the system has drifted.
3. **An accessibility statement** declaring conformance target (WCAG 2.1 AA minimum), known limitations, the date of the most recent audit, and a contact for accessibility issues.

If any of these artifacts is missing, create it as part of your first substantive UI change.

---

## How to Use This File

- Treat these rules as binding for visual and interaction work in this repository. They supersede generic framework defaults where they conflict.
- Section 0 (Accessibility) is the binding floor. No stylistic preference in sections 1–9 overrides it.
- When a task is ambiguous against these rules, ask before proceeding. Do not infer permission from silence.
- When a project's nature genuinely requires a deviation (a reading-first product versus a dense dashboard will set different spacing and type sizes, for example), record the deviation in the design notes document. Same posture, different calibration. Accessibility requirements are not deviation candidates.
- When proposing changes to this file itself, open a PR with the rationale. Do not edit it as part of unrelated work.

## References

- W3C Web Content Accessibility Guidelines (WCAG) 2.1: https://www.w3.org/TR/WCAG21/
- W3C Web Content Accessibility Guidelines (WCAG) 2.2: https://www.w3.org/TR/WCAG22/
- ARIA Authoring Practices Guide (APG): https://www.w3.org/WAI/ARIA/apg/
- ADA Title II 2024 Rule on Web and Mobile Accessibility (DOJ): https://www.ada.gov/resources/2024-03-08-web-rule/
