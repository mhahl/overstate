# Master-config observability — decision record (Draft)

Topic: read-only master-config observability (permission display,
needs-vs-grants self-check, assisted eauth rotation) on RQ background
execution. Full master-config management stays out of scope.

## Status
Draft. Nothing below is accepted until explicitly approved.

## Settled decisions
- D1 (Draft): all long Salt queries move to the worker in this round,
  not just refresh and probes. Sync fallback stays where no worker
  answers.
- D2 (Draft): permission display and self-check extend the existing
  dashboard health panel. No new page.
- D3 (Draft): four probes — key.list_all, manage.status,
  jobs.list_jobs, and test.ping against a single cached minion.
  All read-only; nothing fans out to the fleet.

- D4 (Draft): guided manual rotation ships in this round —
  generated password shown once, master-side and app-side steps,
  verify button, audit-logged, nothing stored.

- D5 (Draft): capability and checklist results cache with a short
  TTL; long operations reuse the existing job-detail poll pattern.
  No new UX language, no new push channel.

## Unresolved
None.
