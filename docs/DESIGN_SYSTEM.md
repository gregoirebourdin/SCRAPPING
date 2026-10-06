# Research — Design System

An operational data workspace: premium, black & white with a violet accent, readable, approachable, fast.
Quality bar: Linear / Attio / Raycast / Clay — without copying any of them.

Source of truth: `packages/design-system/src/tokens.css` (CSS variables) mapped into
Tailwind v4 via `@theme inline` in `packages/design-system/src/theme.css`. Components never
hard-code colors, radii, shadows or font sizes; they use semantic utilities
(`bg-surface-1`, `text-secondary`, `border-subtle`, `text-meta`…).

## 0. Brand

* Name: **Research**. Mark: the geometric "R" (`LogoMark` / `Logo` in
  `packages/design-system/src/brand.tsx`, `currentColor` fill — white on dark, black on light).
* Files: `apps/web/public/brand/research-logo-white.svg`, `research-logo-black.svg`; favicon
  `apps/web/public/icon.svg` (white mark on a black rounded square).

## 1. Color tokens (dark — primary theme)

Black & white base with **one violet accent** (primary actions, focus, selection, progress, active
cell). Other colour is reserved for small status signals (badges, dots), never for chrome. Text
tokens keep ≥ 4.5:1 contrast on their canvas (WCAG AA), muted text included.

| Token | Value | Use |
|---|---|---|
| `--background` | `#000000` | app canvas |
| `--surface-1` | `#0a0a0a` | panels: sidebar, chat, table body |
| `--surface-2` | `#111111` | hover rows, inputs, header row |
| `--surface-3` | `#1a1a1a` | popovers, menus, active items |
| `--surface-overlay` | `rgba(0,0,0,0.7)` | scrim behind dialogs |
| `--border-subtle` | `rgba(255,255,255,0.07)` | grid lines, separators |
| `--border-strong` | `rgba(255,255,255,0.13)` | inputs, popovers |
| `--border-focus` | `rgba(139,143,247,0.6)` | focused inputs, focus ring |
| `--text-primary` | `#fafafa` | primary text |
| `--text-secondary` | `#b3b3b3` | labels, secondary values |
| `--text-muted` | `#8a8a8a` | metadata, placeholders, disabled (6:1 on black) |
| `--accent` | `#8b8ff7` | the ONE accent: primary action, focus, selection, progress, active cell |
| `--accent-strong` | `#a9acfa` | accent text on dark |
| `--accent-soft` | `rgba(139,143,247,0.15)` | selected rows, accent backgrounds |
| `--title-from` → `--title-to` | `#ffffff` → `#8c8c8c` | vertical gradient on titles (`title-gradient`) |
| `--success` | `#5cc995` | SAFE, completed |
| `--warning` | `#d8a656` | RISKY, CATCH-ALL, budget warnings |
| `--danger` | `#e5625f` | INVALID, errors, destructive |
| `--info` | `#62a8de` | running, informational |

Titles (page headers, drawer names, hero and auth headlines) use the `title-gradient` utility:
a vertical gradient clipped to the glyphs, one gradient per line. Light theme: `#000` → `#7a7a7a`.
Avatars are monochrome (foreground at 9–22 % on the canvas).

Status colors are used at low saturation as 8–12 % tinted backgrounds with full-strength
text/dot; never as large fills. A light theme token set exists (`[data-theme="light"]`) for
accessibility, but dark is the designed experience.

## 2. Typography

Self-hosted variable fonts (`@fontsource-variable/*`, no runtime call to Google):

* **Inter** (optical sizing, `cv11` single-storey a) — all UI text: very legible at small sizes.
* **Plus Jakarta Sans** — titles and the wordmark (`font-display`, applied by `title-gradient`): friendlier, more personality.
* **JetBrains Mono** — emails, domains, ids, aligned numbers.

Numbers use `tabular-nums` where they align.

| Token | Size / line / weight | Use |
|---|---|---|
| `text-display` | 24px / 30px | empty-state headlines |
| `text-title` | 16px / 22px / 650 | workspace title (list name), drawer names |
| `text-heading` | 14.5px / 20px / 600 | section headings, dialog titles |
| `text-body` | 14px / 21px / 440 | body, chat (page default) |
| `text-table` | 13px / 18px / 440 | table cells |
| `text-meta` | 12px / 16px / 450 | metadata, badges, kbd, timestamps |
| `text-micro` | 11px / 14px / 550, +0.02em, uppercase | section labels |

No marketing-scale type inside the workspace; the only large headlines are the Discover hero
(34px) and the auth pages (26px), both in Plus Jakarta Sans with the title gradient.

## 3. Spacing, radius, elevation

* 4px grid. Control heights: 24 (xs), 28 (sm, default), 32 (md). Table row: 32 compact,
  40 comfortable. Header row 30. Top bar 44. Sidebar 56 collapsed / 220 expanded.
  Chat panel default 380, resizable 320–520, collapsible.
* Radius: `--radius-xs 3px` (badges, kbd), `--radius-sm 5px` (controls), `--radius-md 7px`
  (popovers, cards, inputs), `--radius-lg 10px` (dialogs, command palette). Nothing pill-shaped
  except status dots and toggles.
* Elevation: borders do the work. Shadows only on floating layers:
  `--shadow-popover: 0 8px 24px -6px rgba(0,0,0,0.55), 0 0 0 1px var(--border-strong)`.
  No glassmorphism beyond a 6px backdrop blur on the command palette scrim.

## 4. Motion

| Token | Value | Use |
|---|---|---|
| `--ease-out` | `cubic-bezier(0.2, 0.8, 0.2, 1)` | default |
| `--dur-1` | 90ms | hover, press |
| `--dur-2` | 150ms | menus, tooltips, sidebar expand |
| `--dur-3` | 220ms | drawers, dialogs, chat panel collapse |

Animation clarifies state only: row hover, resize handles, selection, drawer slide, sidebar
expand, progress, enrichment spinners, tool cards resolving, “+N new leads” pill.
`prefers-reduced-motion` disables transforms.

## 5. Components (packages/design-system)

Primitives (Radix under the hood where interactive): `Button` (primary / secondary / ghost /
danger; xs, sm, md), `IconButton`, `Input`, `Textarea`, `Kbd`, `Badge`, `StatusDot`,
`EmailStatusBadge`, `ConfidenceMeter` (4-segment), `FreshnessBadge`, `Tooltip`,
`DropdownMenu`, `ContextMenu`, `Popover`, `Dialog`, `Sheet` (drawer), `Tabs`, `Switch`,
`Checkbox`, `Separator`, `ScrollArea`, `Skeleton`, `Spinner`, `Toast` (sonner),
`ProgressBar`, `SegmentedControl`.

Badges: 18px tall, `text-meta`, 6px dot + label, tinted background. Email states:
`SAFE` green · `RISKY` amber · `CATCH-ALL` amber outline · `UNKNOWN` muted · `INVALID` red.
Enrichment states: `EMPTY` (faint dash) · `QUEUED` (hollow dot) · `RUNNING` (12px spinner) ·
`DONE` (value) · `UNKNOWN` (“Unknown”, muted italic) · `ERROR` (red dot + reason on hover).
TRUE/FALSE/UNKNOWN booleans render as `✓ Yes` (success), `– No` (secondary), `? Unknown` (muted).

## 6. Layout & screens

```
┌──────┬───────────────────────────────────────────────┬──────────────────────┐
│ 56px │ Top bar: list name · count · search · filter · │ Chat header          │
│ side │ columns · enrich · import · export · more ·    │──────────────────────│
│ bar  │ campaign progress                              │ messages + tool cards│
│      │───────────────────────────────────────────────│                      │
│      │ quality summary strip (optional, 1 line)       │                      │
│      │ table (virtualized, pinned first columns)      │ composer (⌘J)        │
└──────┴───────────────────────────────────────────────┴──────────────────────┘
```

Screens: Discover (empty state “Find your next customers.” + composer + prompt examples;
recent lists/campaigns as compact rows), List workspace, People, Companies, Campaigns
(list + detail with funnel and rejected reasons), Activity (audit + job events), Sources
(health & quality), Usage (cost, cost/lead, modest analytics), Settings (workspace,
budget, models, freshness, suppression, templates, webhooks, members), Account, Review
queue, Sign in / Sign up.

Mobile (< 768px): list viewing as compact cards, chat as full-screen sheet, lead detail;
no spreadsheet editing.

## 7. Interaction rules

* Keyboard: ⌘K palette, ⌘J focus chat, ⌘\ toggle chat, ⌘B toggle sidebar, arrows move the
  active cell, Enter opens/edits, Space toggles selection, ⌘C copies cell/selection, ⌘A select
  all loaded rows, Esc closes drawer/menu/clears selection, `/` focuses table search, `F` filter.
* Selection opens a compact floating action bar (bottom center): Add to list · Move · Remove ·
  Export · Verify · Enrich · Find email · Find person · Refresh · Suppress · Delete · AI.
* Right-click row: Open lead · Add to list · Move · Find email · Refresh (company / person /
  email / column) · Enrich · Copy · Export · Suppress · Delete from list.
* Column header menu: Sort asc/desc · Filter · Pin · Hide · Rename · Duplicate · Enrich
  missing · Refresh · View configuration · Delete.
* Cells: inline edit (stored as user observation), copy, open link, view source, view
  history, confidence indicator on hover — not cluttering the default view.
* Live data never reorders under the user: “+142 new leads” pill → click merges.
* Errors are specific (“Website unreachable”, “Search source rate-limited”, “Email
  verification inconclusive”, “No decision maker found”, “AI enrichment: insufficient
  evidence”) with Retry.

## 8. Quality checklist (applied to every screen before a phase closes)

Looks custom? Clear table hierarchy? Restrained borders? Compact buttons? Consistent spacing?
Chat part of the workspace? Sidebar visually recedes? Feels like a serious $100+/month SaaS?
If not: iterate.
