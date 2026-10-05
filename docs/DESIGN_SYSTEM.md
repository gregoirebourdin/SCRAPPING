# Scout — Design System

An operational data workspace: premium, dark, technical, quiet, precise, dense, fast.
Quality bar: Linear / Attio / Raycast / Clay — without copying any of them.

Source of truth: `packages/design-system/src/tokens.css` (CSS variables) mapped into
Tailwind v4 via `@theme inline` in `packages/design-system/src/theme.css`. Components never
hard-code colors, radii, shadows or font sizes; they use semantic utilities
(`bg-surface-1`, `text-secondary`, `border-subtle`, `text-meta`…).

## 1. Color tokens (dark — primary theme)

| Token | Value | Use |
|---|---|---|
| `--background` | `#0c0d0f` | app canvas (near-black, never `#000`) |
| `--surface-1` | `#111215` | panels: sidebar, chat, table body |
| `--surface-2` | `#16171b` | hover rows, inputs, header row |
| `--surface-3` | `#1c1d22` | popovers, menus, active items |
| `--surface-overlay` | `rgba(8,8,10,0.62)` | scrim behind dialogs |
| `--border-subtle` | `rgba(255,255,255,0.055)` | grid lines, separators |
| `--border-strong` | `rgba(255,255,255,0.10)` | inputs, popovers, focus-adjacent |
| `--text-primary` | `#e9e9ec` | primary text |
| `--text-secondary` | `#a0a1aa` | labels, secondary values |
| `--text-muted` | `#686a74` | metadata, placeholders, disabled |
| `--accent` | `#8b8ff7` | the ONE accent: focus, primary action, selection, progress |
| `--accent-strong` | `#a3a6fa` | accent text on dark |
| `--accent-soft` | `rgba(139,143,247,0.14)` | selected rows, accent backgrounds |
| `--success` | `#4fbf8b` | SAFE, completed |
| `--warning` | `#d9a24a` | RISKY, CATCH-ALL, budget warnings |
| `--danger` | `#e2625f` | INVALID, errors, destructive |
| `--info` | `#62a8de` | running, informational |
| `--focus-ring` | `0 0 0 2px var(--background), 0 0 0 4px rgba(139,143,247,0.55)` | visible focus |

Status colors are used at low saturation as 8–12 % tinted backgrounds with full-strength
text/dot; never as large fills. A light theme token set exists (`[data-theme="light"]`) for
accessibility, but dark is the designed experience.

## 2. Typography

Geist Sans (UI) and Geist Mono (emails, domains, ids, numbers where alignment matters),
`font-feature-settings: "cv11", "ss01"`; numbers use `tabular-nums`.

| Token | Size / line / weight | Use |
|---|---|---|
| `text-title` | 15px / 20px / 590 | workspace title (list name) |
| `text-heading` | 13.5px / 20px / 560 | section headings, dialog titles |
| `text-body` | 13px / 19px / 440 | body, chat |
| `text-table` | 12.5px / 16px / 440 | table cells |
| `text-meta` | 11.5px / 15px / 450 | metadata, badges, kbd, timestamps |
| `text-micro` | 10.5px / 13px / 550, +0.02em, uppercase | section labels |

No marketing-scale type anywhere inside the workspace (the largest text is the empty-state
headline at 20px).

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
