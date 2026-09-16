# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Operators running routine planned work against a Salt-managed fleet: ad-hoc
function runs, state applies, and highstates. Admins share the same flows
plus user/settings management. Viewers see everything and change nothing.

## Product Purpose

Overstate puts a web UI in front of a Salt master. Operators accept keys,
run jobs, apply states, and watch results here; Salt still does the work.
Every button maps to a real Salt call and the UI surfaces the JID so each
action is traceable. Success is a job fired at the right minions with no
surprises.

## Positioning

A thin, honest control surface over one salt-api: snapshot cache for
browsing, live calls for action, destructive operations gated by explicit
type-to-confirm. No multi-master abstraction; one Overstate per master.

## Operating Context

Routine ops usage: planned runs, repeat jobs, saved jobs and presets
(test.ping, state.apply, highstate, dry-run). Roles gate actions
(viewer < operator < admin); buttons above the user's role do not render
and the server rejects forged requests. Local password login plus OIDC
SSO (new OIDC users land as viewers until an admin promotes them).
Targets: glob, list, grain, compound, nodegroup, saved group.
Transports: local (async default) and salt-ssh (sync only, 180s timeout,
synthetic JID).

## Capabilities and Constraints

- /jobs/new fields: target + target type, function, args, batch
  mode/size, save-as, transport, async/sync, destructive confirm gate.
- Wave batches with a failure gate and cancel flag; state.orchestrate
  runs with stored returns; job kill, returner sync, and live SSE
  stream on the detail page.
- Minion inventory with key states and presence, detail tabs
  (grains, states, jobs, schedule, pillar, beacons, mine), guided
  onboarding scripts, beacon toggles, CSV export.
- Keys accept/reject, saved minion groups as first-class targets,
  mine browser, pillar and SLS file browsers, schedules, state
  conformity watch, live event stream, audit trail.
- Saved minion groups are first-class targets, managed on /groups/.
- Fleet-wide or slow Salt calls run on an RQ worker (Redis transport)
  with a synchronous inline fallback; the stack works without the
  worker but slower.
- Stack is Flask + Jinja + HTMX + Alpine + daisyUI (wireframe theme),
  Postgres, Redis; no new framework.
- Production target is Kubernetes (manifests in deploy/kubernetes);
  dev runs on compose. Licensed Apache-2.0.

## Brand Commitments

Overstate name; daisyUI component conventions; wireframe theme.
No logo, custom font, or marketing voice to preserve.

## Evidence on Hand

- `docs/user.md`: operator guide and role model.
- `docs/install-kubernetes.md`: production install walkthrough.
- `docs/architecture-kubernetes.md`: design, assumptions, risks.
- `docs/developer.md`: conventions, tests, lint, coverage, deps.
- Full pytest suite (266 tests) covering job flows, batches,
  auth/RBAC, inventory, and deploy artifacts.

## Product Principles

1. Every control maps to a real Salt call; nothing decorative fires.
2. Show the blast radius before firing: target, transport, and mode.
3. Routine work should be repeatable: presets and saved jobs first-class.
4. destructive actions stay deliberately hard (type-to-confirm).

## Accessibility & Inclusion

No product-specific standard established. Baseline: labels for inputs,
keyboard-operable controls, visible focus, no color-only meaning.
