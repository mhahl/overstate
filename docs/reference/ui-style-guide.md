# UI style guide

Overstate uses Tailwind CSS and daisyUI components, with theme tokens as the source of truth for surfaces, text, and controls. The shared shell and operational pages should feel like one application: navigation, settings, minion onboarding, and the master console use the same spacing, surface, focus, and action treatments.

## Shared visual rules

- Keep the selected daisyUI theme for user control. The built-in `wireframe` choice is softened with a cool neutral surface palette, a clear blue primary action color, and rounded fields/cards; it no longer overrides the rest of the application with flat grayscale controls.
- Use a translucent sticky top bar, restrained sidebar section labels, and a tinted active navigation item. Do not change destinations, role checks, or the command palette.
- Use the shared card surface, rounded corners, subtle borders/shadows, and consistent tab treatment for settings and operational panels.
- Keep keyboard focus visible on inputs and preserve reduced-motion preferences.
- The console output remains a distinct dark terminal surface, while its surrounding card and input area follow the selected application theme.

## List tables and toolbars

List pages share one shell: a `card card-border bg-base-100` surface holding the search toolbar, the table, and the pager. The card is what makes the toolbar white; a toolbar sitting directly on the page renders on the grey `base-200` background and looks detached from its table.

- Put the toolbar inside the card, separated from the table by `border-b border-base-300 px-4 py-3`.
- When a page holds several tables in separate cards (as the Encrypted pillar page does per collection), give the shared toolbar its own card with a `card-body p-3`. Do not leave it loose on the page.
- Build the toolbar from the shared macros — `m.table_search_field`, `m.page_size_select`, `m.pagination_plain` / `m.pagination_hx` — rather than hand-rolling a form.
- A table with several independent pagers passes `page_param` so each pager drives its own query parameter.

`tests/ui_assertions.py` enforces the surface rule: `test_table_searches_sit_on_a_card_surface` (and the secrets page's own test) fail when a `role="search"` form has no `card bg-base-100` ancestor.

## Scope of the UX polish

Visual-only changes in `assets/app.css` and `overstate_ui/templates/base.html` apply to all pages, including settings, onboarding, and console. They change colors, surface treatment, typography, focus indication, and transitions only. Form fields, routes, authorization, job behavior, settings values, and onboarding/console workflows are unchanged by this styling work.

Build the checked-in stylesheet with `npm run build:css`. Verify it and the shared UI contract with `pytest tests/test_ui_consistency.py`.
