# Master-config observability — decision record (Final)

Topic: read-only master-config observability (permission display,
needs-vs-grants self-check, assisted eauth rotation) on RQ background
execution. Full master-config management stays out of scope.

## Status
Final. Accepted by the owner 2026-09-21. Implementation needs a
separate explicit request; accepting this record approves no code,
tests, or commits.

## Settled decisions
- D1 (Final): narrowed 2026-09-21 (accepted the recommendation) — only
  the observability surface moves to the worker in this round
  (dashboard probes, master-config reads, checklist queries), not all
  long Salt queries. Per-tab inline calls stay: they are
  timeout-bounded and already degrade gracefully per tab. A
  fleet-wide worker migration is a separate plan with its own
  interview. Sync fallback stays where no worker answers.
- D2 (Final): permission display and self-check extend the existing
  dashboard health panel. No new page.
- D3 (Final): four probes — key.list_all, manage.status,
  jobs.list_jobs, and test.ping against a single cached minion.
  All read-only; nothing fans out to the fleet.

- D4 (Final): scoped 2026-09-21 (accepted the recommendation) —
  guided manual rotation covers the salt-api eauth password only
  (shared by the masters' api.conf and the app). Generated password
  shown once, master-side and app-side steps, verify button,
  audit-logged, nothing stored. DB and returner credentials stay out;
  a second credential type needs its own decision.

- D5 (Final): settled 2026-09-21 (accepted the recommendation) —
  checklist results reuse the existing 300s capability TTL; one TTL
  for the whole panel. Long operations reuse the existing job-detail
  poll pattern. No new UX language, no new push channel.

## Unresolved
None.
