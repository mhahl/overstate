# Overstate Design System (reverse-engineered)

> Codified from the code as of 2026-10. Every claim cites a file path. Usage counts come from grepping `overstate_ui/templates/`. Where the code contradicts itself, both usages are shown and labelled **CONFLICT**.

## 1. Overview

### Stack & styling approach

| Concern | Choice | Evidence |
|---|---|---|
| CSS | Tailwind CSS v4, `@import "tailwindcss"` | `assets/app.css:1` |
| Components | daisyUI 5 plugin, themes `corporate --default, dark` | `assets/app.css:2-4` |
| Icons | `@iconify/tailwind4`; lucide slugs via `icon-[lucide--name]`, plus `simple-icons` for OS logos | `assets/app.css:5`, `base.html:20-21` |
| Templates | Jinja2, one file per page, `_`-prefixed partials | `overstate_ui/templates/` |
| JS | htmx + Alpine loaded globally, used sparingly (6 templates use `hx-`); vanilla inline IIFEs for widgets | `base.html:9-10` |
| Custom CSS | **Almost none.** `app.css` only styles what utilities cannot express: Pygments `codehl`, CodeMirror editor, Wunderbaum tree, `#term` console, `:focus-visible`, reduced-motion. Everything else is utility classes in markup | `assets/app.css:23-185` |

**Rule: never add a CSS class; reach for daisyUI components + Tailwind utilities first.** The one sanctioned exception is binding a third-party widget to daisyUI token variables, which `app.css` already does (`#file-tree`, `#editor-mount`).

### Directory map

- `assets/app.css` — the entire stylesheet (185 lines).
- `overstate_ui/templates/base.html` — app shell: drawer layout, navbar, sidebar, flash banners, global JS (pending-spinners, palette, copy/download).
- `overstate_ui/templates/_macros.html` — **the shared component library** (see §3).
- `overstate_ui/templates/_confirm.html` — the global destructive-confirm dialog.
- `overstate_ui/templates/_pillar_tabs.html`, `_users_tabs.html` — tab-list partials used across pages.
- `overstate_ui/templates/_*.html` — everything else is a page-specific partial (`_job_rows`, `_minion_rows`, `_grant_dialog`, `_dashboard_live`, …).
- `tests/test_ui_consistency.py` — **executable design rules**; treat its assertions as normative (they run in CI).

## 2. Design tokens

There is **no custom token file**; tokens are the daisyUI defaults plus the built-in `corporate` theme. Use daisyUI semantic names — never raw hex in markup, never `dark:` variants (the theme system handles dark mode).

### Colors (semantic, from daisyUI)

| Token | Used for | Example |
|---|---|---|
| `base-100` | Card / raised surface background | every card |
| `base-200` | Page background; tinted wells (tree pane, code blocks) | `base.html:14`, `files.html:95` |
| `base-300` | Hairline borders | `border-base-300` ~30× |
| `base-content` | Body text | default |
| `primary` | Primary action; focus ring; active states | `btn-primary`, `app.css:167` |
| `success` / `warning` / `error` / `info` | Semantic state ONLY (badges, alerts). **Not decoration** | `_confirm.html:18` |
| `neutral` | Terminal surface (`#term`) | `app.css:172` |

**Muted text convention:** secondary text is `text-base-content/70`; tertiary is `/60` (bumped from `/60`→`/70` in low-contrast spots, e.g. `files.html:77`). Opacity `opacity-50`/`opacity-70` also appears inline (`base.html:93`, `audit.html:37`).

### Typography

| Style | Usage | Evidence |
|---|---|---|
| `font-mono` | **Everything technical**: paths, keys, SHAs, IPs, JIDs, fingerprints, command names | 51 of ~60 templates use it |
| Default sans | Headings, labels, body copy | `base.html` |
| `text-2xl font-bold tracking-tight` | Page H1 | `_macros.html:3` (`page_heading`) |
| `text-base` card-title | Card H2 | `settings.html:11` |
| `text-sm` | Body/table-secondary | pervasive |
| `text-xs` | Table cell data, metadata, hints | `audit.html:37-43` |
| `tabular-nums` | Any numeric column/count | 16 templates |

There is no custom font scale — the Tailwind defaults are the scale.

### Spacing & rhythm

- Page container: `p-4 lg:p-6 w-full max-w-7xl mx-auto` (`base.html:61`).
- Vertical rhythm is **the Tailwind 4px base**: card gaps `mb-4`, card-body gaps `gap-3` or `gap-4`, toolbar padding `px-4 py-3`.
- Section/page headers: `page_heading` then actions, `mb-4` before content (`pillar_secrets.html:5-9`).
- Cards separated by `mb-4`; grid layouts `gap-4`.

### Breakpoints (usage-ranked)

- `lg:` — primary breakpoint: drawer becomes persistent (`lg:drawer-open`, `base.html:16`), nav toggles hide, content padding bumps.
- `xl:` — two-pane detail splits: `xl:grid-cols-12` with `xl:col-span-7` / `xl:col-span-5` (7+7 occurrences; `pillar_detail.html:54,92`).
- `md:` — rarely used; stats row orientation (`md:stats-horizontal`).

### Radii / shadows / z-index

- Radii: daisyUI defaults via `rounded-box`, `rounded-full` (avatars/status dots), `card` radius. No custom values.
- Shadows: daisyUI default `card` elevation. **No `shadow-*` overrides found in templates.**
- Z-index layers (ascending): content → navbar `z-30` (`base.html:19`) → drawer side `z-40` (`base.html:96`) → skip-link/popovers `z-50` (`base.html:15,48`). **Do not invent new layers; reuse these.**

## 3. Component catalogue

Every reusable piece lives in `_macros.html` (call via `{% import "_macros.html" as m %}`) unless noted.

### 3.1 Buttons

**Canonical variants** (usage counts from grep):

| Variant | Count | Use |
|---|---|---|
| `btn btn-sm` | 215 | default size, all contexts |
| `btn btn-xs` | 17 | row-level actions inside tables |
| `btn-primary` | — | THE single primary action on a page/panel (Filter, Save, Create, Run) |
| `btn-ghost` | — | secondary navigation, table-row actions, Cancel in dialogs |
| `btn-outline` (+`border-base-300`) | — | neutral prominent action (Add, Wire, Manage) |
| `btn-error` | — | destructive confirm button only (`_confirm.html:18`) |
| `btn-error btn-outline` | — | destructive row action (Delete grant, `_grant_dialog.html:26`) |

```html
<!-- canonical trio: toolbar primary, ghost secondary, error destructive -->
<button class="btn btn-sm btn-primary mb-1">Filter</button>
<a class="btn btn-sm btn-ghost mb-1" href="...">Clear</a>
<button class="btn btn-sm btn-error btn-outline join-item">Delete</button>
```
Source: `audit.html:25-26`, `keys.html:73`.

**Rules**
- **`mb-1` in `items-end` toolbars:** daisyUI fieldsets carry `.25rem` bottom padding, so a button beside fieldset-wrapped inputs needs `mb-1` to sit flush. Every toolbar does this; the page-size macro now does it too (`_macros.html:107`).
- **One `btn-primary` per region.** The Filter button is primary on every filter page — enforced by `tests/test_ui_consistency.py:65-70`.
- Buttons always carry an icon + text, or text alone; icon-only buttons get `aria-label` (`base.html:33`).
- Slow actions get `data-loading-link`/`data-loading-form` so the global spinner fires (`base.html:232-265`).

### 3.2 Badges

**Canonical:** `badge badge-sm` (147×). Sizes: `badge-sm` standard, `badge-xs` for row cells. `badge-soft` is **deprecated** — use solid modifiers only.

| Tone | Use | Evidence |
|---|---|---|
| `badge-success` | positive state (encrypted, wired, present, accepted) | `pillar_secrets.html` |
| `badge-warning` | needs-attention (not wired, pending, missing) | `keys.html:7` |
| `badge-ghost` | neutral metadata/counts (revision, page counts, roles) | `files.html:28-29` |
| `badge-outline` | secondary neutral (role chip in navbar) | `base.html:46` |
| `badge-error` | only for genuinely broken states | rare (1×) |

**Rule:** a badge is a **status**, not a label. If the state is obvious from context (e.g. a value is obviously encrypted on the "Encrypted pillar" page), don't badge it. State = solid modifier; neutral = `badge-ghost`. No `badge-soft` (enforced by `test_ratchet_deprecated_visual_classes`).

### 3.3 Alerts / banners

`alert` + `role` + icon, full width above content, `mb-4`:

```html
<div class="alert alert-warning mb-4" role="status">
  <span class="icon-[lucide--triangle-alert] size-5 shrink-0" aria-hidden="true"></span>
  <span>{{ m }}</span>
</div>
```
Source: `base.html:73-77` (flash banner).

Frequencies: `alert-warning` 36, `alert-error` 19, `alert-info` 10, `alert-success` 3. Icons are fixed per severity: success `circle-check`, warning `triangle-alert`, error `circle-x`, info `info` (`base.html:68-88`). `role="alert"` for errors, `role="status"` for everything else.

### 3.4 Cards

**Canonical shell** (46×):

```html
<section class="card card-border bg-base-100 min-w-0" aria-label="...">
  <div class="flex flex-wrap items-end justify-end gap-2 border-b border-base-300 px-4 py-3">
    ...toolbar (search + filter + page size)...
  </div>
  <div class="max-w-full overflow-x-auto rounded-box">
    <table class="table table-sm table-pin-rows w-full"> ... </table>
  </div>
</section>
```
Source: `audit.html:9-50`, `users.html:11-37`, `pillar.html:10-23`.

**Rules**
- **The card is the only frame.** Tables sit in `rounded-box` with **no inner border**. (Two files still use `rounded-box border border-base-300` — `groups.html:32`, historical `formulas.html:99` — **CONFLICT**, old pattern.)
- `aria-label` (or `aria-labelledby`) on every `section.card`.
- Toolbar row always `border-b border-base-300 px-4 py-3`; pagination footer always `border-t`.

### 3.5 Search / filter toolbar

Composed from three macros, always in this order, in an `items-end justify-end` row:

```html
{{ m.table_search_field("audit-search", "q", q, "Search", "Search …", "Search …") }}
<button class="btn btn-sm btn-primary mb-1">Filter</button>
{% if q %}<a class="btn btn-sm btn-ghost mb-1" href="...">Clear</a>{% endif %}
{{ m.page_size_select('audit.index', per_page, {...params}) }}
```
Source: `_macros.html:96-114` (macro), `audit.html:24-28` (usage).

- The search `<form>` carries `role="search"` and an `aria-label`; the input is `type="search"` inside a daisyUI `input input-sm`, and **selects all on focus** (`onfocus="this.select()"`) so a stale query is replaced in one keystroke (`_macros.html:96-104`).
- Optional pre-filters (role, status, collection) go as a `fieldset`/`label` + `select select-sm` **before** the search field, with `onchange="this.form.requestSubmit()"` (`users.html:16-29`).
- **All filter state survives pagination** — the page-size and pagination macros take a `params` dict; pass every active filter into it.
- The toolbar sits on a card surface (`card bg-base-100` ancestor) — enforced by `tests/ui_assertions.py`.

### 3.6 Tables

**Canonical table** (enforced by `tests/test_ui_consistency.py`):

```html
<div class="max-w-full overflow-x-auto rounded-box">
  <table class="table table-sm table-pin-rows w-full">
    <thead><tr>{{ m.sort_th_plain('audit.index', 'when', 'When', sort, direction, {...params}) }}<th scope="col">Outcome</th>...</tr></thead>
    <tbody>
      <tr class="hover">
        <td class="whitespace-nowrap text-xs opacity-70">{{ e.created_at|reltime }}</td>
        <td class="font-medium">{{ e.user }}</td>
        <td class="font-mono text-xs">{{ e.action }}</td>
        ...
      </tr>
    </tbody>
  </table>
</div>
```
Source: `audit.html:31-50`.

**Anatomy rules**
- Wrapper `max-w-full overflow-x-auto rounded-box`; table `table table-sm table-pin-rows w-full`; **no** `table-zebra` (enforced: `test_ui_consistency.py:119-127`).
- Sortable headers use `m.sort_th_plain` / `m.sort_th` — they set `aria-sort` and arrow icons (`_macros.html:12-28`). Plain columns get `scope="col"` (added to every table).
- Monospace/technical data: `font-mono text-xs`; long paths `break-all`; short cells `whitespace-nowrap`; numbers right-aligned `tabular-nums`; timestamps via `{{ x|reltime }}` (13 templates) or `{{ x|filesizeformat }}` (`files.html`).
- Row hover: `class="hover"` on `<tr>`.
- Row actions: right-aligned `<td class="text-right">` with `btn btn-xs btn-ghost` buttons, or a `join join-horizontal` for accept/reject/delete sets (`keys.html:56-73`).
- Destructive row actions are forms with `data-confirm="..."` (`keys.html:62`).

**Pagination** (`_macros.html:30-61`): `m.pagination_plain(endpoint, page, pages, params, label)` — numbered `join` pager with Prev/Next, ellipsis, `aria-label="Go to page N"`, `aria-current="page"`. Footer row: `border-t border-base-300 px-4 py-3` with a "`X–Y of Z`" count line (`files.html:177-180`). Page-size: `m.page_size_select` (sizes 10/25/50/100).

**Empty state** (canonical, 22× `py-8 text-center`):

```html
<div class="flex flex-col items-center gap-2 px-4 py-10 text-center">
  <p class="font-medium">No files found</p>
  <p class="text-sm text-base-content/70">...why + what to do...</p>
  <a class="btn btn-sm btn-ghost">Back to all files</a>
</div>
```
Source: `files.html:160-167`. Optional icon in a `size-10 rounded-full bg-base-200` circle above the title (`files.html:161`).

### 3.7 Tabs

Canonical: tabs are a **standalone partial included above the card**, not inside it:

```html
{% include "_pillar_tabs.html" %}   {# renders <div role="tablist" class="tabs tabs-border mb-4"> #}
<section class="card ...">
```

Source: `_pillar_tabs.html:6-13`, `_users_tabs.html:6-13`, used by `pillar.html:9`, `users.html:10`, `jobs.html`, `minion_detail.html`, `formulas.html`, `keys.html`.

Tab anchor form: `class="tab gap-2"` + `tab-active` + `aria-current="page"` on the active tab, optional `badge badge-sm` count inside the tab. Tabs are always **outside the card** (enforced by `test_ratchet_tabs_live_outside_cards`). The jobs tab bar is plain links above the card — the htmx swap region contains only the table body.

### 3.8 Modals / dialogs

Canonical (HTML `<dialog>`, not checkbox/anchor modals):

**Dialog opener:** form dialogs open through the shared `openDialog('id')` helper (`base.html`), which shows the modal and focuses the first field:

```html
<button class="btn btn-sm btn-outline" onclick="openDialog('{{ dialog_id }}')">Grants</button>
<dialog id="{{ dialog_id }}" class="modal" aria-labelledby="{{ dialog_id }}-title">
```
Source: `_grant_dialog.html:5-85`, `pillar_secrets.html` (three dialogs).

- `modal-action` footer: **Cancel (ghost) left, primary right**.
- Field groups inside a dialog: `fieldset` + `fieldset-legend` (`_grant_dialog.html:40-47`).
- Per-subject dialogs use a macro (`grant_dialog`) or one shared dialog populated from `data-*` attributes on the trigger (`data-edit-secret`, `pillar_secrets.html`).
- Dialogs that must not steal focus from their own logic set `data-no-autofocus` or open via their own script (the global confirm, edit-secret, minion removal, palette).

**Global confirm dialog** (`_confirm.html`) — the standard for destructive/irreversible actions:

- Attach `data-confirm="message"` to a form; submit is intercepted once, Confirm resubmits.
- For "blind/irreversible" actions, add `data-confirm-checkbox="label"` — the user must tick the box; a hidden `confirm_box=1` is then posted and the **server re-checks it** (`pillar_secrets.py` `secret_update`).
- Buttons may carry the attributes directly (`pillar_secrets.html:130`).
- Confirm button is `btn-error`; focus moves to Cancel on open (`_confirm.html:66`).

### 3.9 Forms

Canonical field pattern (daisyUI `fieldset`, label above control):

```html
<label class="fieldset grow min-w-40">
  <span class="fieldset-legend">Key (dotted)</span>
  <input name="key" class="input input-sm font-mono w-full" required aria-label="Secret key for {{ c.name }}">
</label>
```
Source: `pillar_secrets.html` (add/edit dialogs), `_grant_dialog.html:40-75`, `settings.html:15-32`.

- Labels: `fieldset-legend` (bold) or `label`; every control must have a visible label or `aria-label` — **enforced** by `test_ui_consistency.py:130-160`.
- Sizes: `input input-sm` (52×), `select select-sm` (33×), `textarea ... font-mono` (settings multiline). `input-sm`/`select-sm` are the only sizes for form controls; `btn-xs` is reserved for row actions and never paired with `xs` inputs (enforced by `test_ratchet_deprecated_visual_classes`).
- Submit/cancel: in-page forms put Save/primary bottom-right (`settings.html:40`); dialogs use `modal-action` (see §3.8).
- Secrets: `type="password"`, `autocomplete="new-password"`, placeholder "Unchanged" when a stored value is not echoed (`settings.html:26`).
- Settings page groups fields into section cards, one `<form>` per section, saving only its own keys (`settings.py` `save`).

### 3.10 Navigation & shell

- **Drawer shell**: `drawer lg:drawer-open` — off-canvas below `lg`, persistent sidebar at `lg+` (`base.html:16-17`).
- **Sidebar** (`base.html:108-190`): `menu w-full gap-0.5`, grouped by `menu-title` (Operate / Configure / Observe / Security / System), each item an icon (`icon-[lucide--*] size-4.5`) + label, `menu-active` + `aria-current="page"` on the current section.
- **Navbar** (`base.html:19-59`): sticky, `z-30`; right side holds theme toggle, command palette button (`⌘K`), and the global primary action ("Run job").
- **Command palette** (`base.html:268+`): modal with `aria-label`, search input labelled "Pages, presets, minions…".
- **Skip link**: `sr-only focus:not-sr-only` to `#main-content` (`base.html:15`); main has `tabindex="-1"` (`base.html:61`).

### 3.11 Misc primitives (existing, don't reinvent)

- `stats stats-vertical md:stats-horizontal` — metric panels (`minion_detail.html:27`, `_dashboard_live.html`).
- `join join-horizontal` — grouped row actions / role-select+Set (`users.html:56-66`, `keys.html`).
- `kbd kbd-xs` — keycap hints in the navbar only (`base.html:36`).
- `menu` — sidebar + in-card lists (`formulas.html` untracked list uses `list`/`list-row`).
- `loading loading-spinner loading-xs` — the only loading indicator; always `aria-hidden` + text (`base.html:211`).
- `checkbox checkbox-sm` / `toggle toggle-sm` — confirm gates and the theme toggle (`_confirm.html:13`, `base.html:31`).

## 4. Patterns

### 4.1 The list-page skeleton (the dominant template)

Every "list" page follows this exact stack (top to bottom):

1. **Page header**: `m.page_heading(title, blurb)` + action buttons in `flex flex-wrap items-center gap-3 mb-4` (or `items-end` when aligned to controls). Primary action on the right.
2. **Optional tabs partial** (`_pillar_tabs`/`_users_tabs`), outside the card.
3. **Optional status/alert banners** above the card.
4. **One card** (`card card-border bg-base-100 min-w-0`):
   - toolbar row (`border-b border-base-300 px-4 py-3`): filters + search + `Filter`/`Clear` + page-size
   - flush `rounded-box` table (`table table-sm table-pin-rows w-full`)
   - empty state or pagination footer (`border-t`)
5. **Dialogs** after the card (or macros).

Reference implementations: `audit.html`, `users.html`, `pillar.html`, `files.html`, `minions.html`, `keys.html`, `pillar_secrets.html`.

### 4.2 Button & action placement

| Action type | Placement |
|---|---|
| Single primary action | page header right, or toolbar right (`btn-primary`) |
| Filter | toolbar, after search (`btn-primary mb-1`) |
| Clear filters | toolbar, ghost, only when a filter is active |
| Row-level edit/other | `btn btn-xs btn-ghost` in a right-aligned Actions cell |
| Row-level destructive | form + `data-confirm`, `btn-xs btn-ghost` or `btn-error btn-outline`, in the same Actions cell |
| Dialog confirm | `modal-action`: Cancel ghost left, primary right |
| Irreversible/global destructive | form + `data-confirm` (+ `data-confirm-checkbox` when consequences are severe) |

Buttons use only `primary` / `ghost` / `outline` / `error` — never state colors (`warning`, `success`, `info`).

### 4.3 Table anatomy

Column order, by information priority: identity (name/id) → derived/technical fields → status badges → right-aligned Actions. Sorting on stable columns via `sort_th_plain`; `aria-sort` reflects state. No bulk selection exists anywhere; no column visibility toggles; no drag/drop.

### 4.4 Feedback hierarchy

1. **Flash banners** (top of main) for route-level results: success/warning/error/info with fixed icons.
2. **Inline alert cards** for persistent page state (e.g. "Salt API unreachable", `minions.html:20-24`).
3. **Confirm dialog** before destructive/irreversible submits.
4. **Pending spinners**: global submit handler disables buttons and spins the pressed one (`base.html:543+`); `data-loading-link` for slow GETs.
5. **Empty states** with an explanation and a recovery action.

## 5. Anti-patterns & inconsistencies

Resolved in this pass (now enforced by `test_ratchet_*`):

1. ~~In-card tabs on jobs/minion_detail/formulas~~ — **fixed**: all tabs now sit above the card.
2. ~~Bordered inner table wrapper (`groups.html`, `minion_detail.html`, `schedules_all.html`)~~ — **fixed**: tables use borderless `rounded-box`; the card is the only frame. Code viewers (`file_view.html`, `reactor_view.html`) and the file tree keep their borders as content surfaces.
3. ~~Filter margin drift (`keys.html` `mb-0.5`)~~ — **fixed**: all toolbar Filter/Clear buttons use `mb-1`.
4. ~~`file_sort_th` private macro in `files.html`~~ — **fixed**: `sort_th_plain`/`sort_th` now take `dir_param` (default `"dir"`); files.html uses `dir_param="sd"` for its folder-path collision. The shared macro is the only sort header.
5. ~~`btn-warning` (masterconfig, job_confirm)~~ — **fixed**: replaced with `btn-primary`; state colors on buttons are not used.
6. ~~Inline `onclick="document.getElementById('x').showModal()"`~~ — **fixed**: form dialogs open via the shared `openDialog('id')` helper in `base.html`, which also moves focus to the first field. Destructive confirms (`_confirm.html`) manage their own focus and are exempt.
7. ~~`badge-soft`~~ — **deprecated**: solid modifiers only.
8. ~~`input-xs`/`select-xs` (formulas track rows)~~ — **fixed**: migrated to `sm`; `btn-xs` remains for row actions only.
9. ~~Sidebar `menu-active` without `aria-current`~~ — **fixed**: every nav item that can be active also declares `aria-current="page"`.

Still open: `minion_detail.html` uses `<div class="card">` where other pages use `<section class="card">` — semantics differ but the visual pattern is identical; accept as-is (documented, not a defect).

## 6. Agent-facing rules (normative checklist)

When extending the UI:

- **New list view:** copy the §4.1 skeleton — `page_heading` header → optional tab partial → one `card card-border bg-base-100 min-w-0` → toolbar `border-b border-base-300 px-4 py-3` → `max-w-full overflow-x-auto rounded-box` → `table table-sm table-pin-rows w-full` → pagination footer `border-t`. Do not put tabs inside the card. Do not add an inner border to the table wrapper.
- **Search:** use `m.table_search_field` inside a `role="search"` form; put the form on a card surface (`card bg-base-100` ancestor) — enforced by `tests/ui_assertions.py`.
- **Filtering:** submit button is `btn btn-sm btn-primary mb-1`; Clear is `btn btn-sm btn-ghost mb-1` shown only when filters are active; page-size is `m.page_size_select` with the full filter `params` dict passed through.
- **Sorting:** use `m.sort_th_plain` (or `m.sort_th` inside htmx regions); never hand-roll sortable headers. When a page already uses `dir` for something else (e.g. folder navigation), pass `dir_param` to the macro instead of writing a private header. Non-sortable `<th>`s get `scope="col"`.
- **Table cells:** technical values `font-mono text-xs`; numbers `tabular-nums`; timestamps `|reltime`; long paths `break-all`; row hover `class="hover"`; row actions right-aligned `text-right` with `btn btn-xs`.
- **Empty state:** centered `py-8/py-10 text-center` block with `font-medium` title, muted explanation, and one ghost action. Never a bare "No data" string.
- **Destructive action:** wrap in a form with `data-confirm="specific message naming the subject"`; use `data-confirm-checkbox` + server `confirm_box` re-check for irreversible/blind-overwrite cases; Confirm button is `btn-error`.
- **Dialog:** HTML `<dialog class="modal">` + `modal-box` + `modal-action` (Cancel ghost left, primary right) + `modal-backdrop` form. `aria-labelledby` on the dialog. Open form dialogs with `onclick="openDialog('id')"` — never hand-roll `showModal()`.
- **New page heading:** `m.page_heading(title, blurb)`. Never a bare `<h1>`.
- **Badges:** `badge badge-sm` for status, colored only for semantic state (`success`/`warning`/`error`); `badge-ghost` for neutral meta. No `badge-soft`. Never badge something obvious from context.
- **Buttons in `items-end` rows beside fieldsets:** add `mb-1`.
- **Card section naming:** if the card has a visible heading, use `aria-labelledby` pointing at it; if it's a pure container (no internal heading), use `aria-label`.
- **Colors:** daisyUI semantic tokens only; no hex, no `dark:` variants, no custom CSS selectors (exceptions: binding third-party widgets to tokens in `app.css`).
- **Icons:** `icon-[lucide--name]` with visible text, or `aria-label` when icon-only; `aria-hidden="true"` on decorative icons.
- **Timestamps:** `reltime` filter (registered in `timefmt.py`) renders `<time>` with title tooltip — do not hand-format dates.
- **Validation:** visible `fieldset-legend`/`label` or `aria-label` on every input/select/textarea — `test_ui_consistency.py` fails otherwise.
- **Don't**: `table-zebra`; nested cards (`card` inside `card` — use `border-t` separation, `pillar_secrets.html:82`); custom breakpoints or z-index values; `transition-all`; `badge-soft`; `btn-warning`; `input-xs`/`select-xs`; inline `showModal()`; inner table borders.

Run `pytest tests/test_ui_consistency.py` after template changes — it encodes many of these rules.

## 7. Open questions

Resolved in this pass (see §5 for the fixes):

1. ~~Tab placement for jobs/minion_detail/formulas~~ — **resolved**: tabs live outside the card everywhere. Jobs' tabs are plain links above the card; the htmx region contains only the table body.
2. ~~`mb-0.5` vs `mb-1` on keys.html~~ — **resolved**: `mb-1`.
3. ~~Badge soft-vs-solid~~ — **resolved**: solid modifiers only; `badge-soft` deprecated.
4. ~~Dense row-edit forms (`input-xs` in formulas track row)~~ — **resolved**: `sm` sizes only; `btn-xs` remains for row actions.
5. ~~`aria-label` vs `aria-labelledby` for sections~~ — **resolved**: visible heading → `aria-labelledby`; pure container → `aria-label`.

Remaining (accepted, documented): `minion_detail.html` wraps its card in `<div class="card">` where other pages use `<section class="card">` — same visual pattern, different semantics; not a defect.

## 8. Enforcement ratchet

The following are pinned by `tests/test_ui_consistency.py` so a future edit cannot silently reintroduce them:

- `test_ratchet_deprecated_visual_classes` — bans `badge-soft`, `btn-warning`, `input-xs`, `select-xs`, and bordered table wrappers (code viewers and the file tree are exempt because they are content surfaces, not tables).
- `test_ratchet_tabs_live_outside_cards` — tabs must precede the card on jobs/minion_detail/formulas.
- `test_ratchet_sidebar_items_declare_current_page` — every `menu-active` nav item pairs with `aria-current="page"`.
- `test_ratchet_form_dialogs_open_with_focus` — form dialogs open via `openDialog('id')`; no page hand-rolls `showModal()`.
- `test_ratchet_search_fields_select_on_focus` — the shared search field selects its contents on focus.

These join the pre-existing rules (table pinning, no zebra, Filter-primary, labelled controls, search-on-card) as the executable contract. Run `pytest tests/test_ui_consistency.py` after any template change.
