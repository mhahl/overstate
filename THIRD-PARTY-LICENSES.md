# Third-party licenses

Overstate itself is Apache-2.0 (see `LICENSE`). This file lists the
open-source packages it depends on, with the license each one declares
in its own distribution metadata. Versions match `requirements.lock`
and `package-lock.json`.

## Python runtime

| Package | Version | License |
|---|---|---|
| Flask | 3.1.3 | BSD-3-Clause |
| Flask-Login | 0.6.3 | MIT |
| Flask-WTF | 1.3.0 | BSD-3-Clause |
| SQLAlchemy | 2.0.52 | MIT |
| Alembic | 1.20.0 | MIT |
| psycopg (+binary) | 3.3.5 | LGPL-3.0-only |
| redis | 8.1.0 | MIT |
| rq | 2.12.0 | BSD-2-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| argon2-cffi | 25.1.0 | MIT |
| Authlib | 1.8.0 | BSD-3-Clause |
| gunicorn | 26.2.0 | MIT |
| PyYAML | 6.0.3 | MIT |

Dev-only (not shipped in the image): pytest (MIT), pytest-cov (MIT).

## JavaScript build-time

| Package | Version | License |
|---|---|---|
| tailwindcss | 4.3.3 | MIT |
| @tailwindcss/cli | 4.3.3 | MIT |
| daisyui | 5.7.37 | MIT |
| @iconify/tailwind4 | 1.2.3 | MIT |
| @iconify-json/lucide | 1.2.132 | ISC |
| @iconify-json/simple-icons | 1.2.96 | CC0-1.0 |
| codemirror | 6.0.2 | MIT |
| @codemirror/lang-yaml | 6.1.3 | MIT |
| esbuild | 0.28.2 | MIT |
| wunderbaum | 0.14.1 | MIT |

## Compatibility verdict: no conflicts

MIT, BSD-2-Clause, BSD-3-Clause, ISC, and CC0-1.0 are permissive and
combine with Apache-2.0 without conditions beyond keeping their
notices, which this file does.

psycopg is the one copyleft package (LGPL-3.0-only). It is used
unmodified as a separate installed package, which the LGPL permits
alongside Apache-2.0 code: keep it replaceable (it is, via pip),
keep its notices (above), and point at upstream sources
(https://www.psycopg.org) on request. Nothing in this repo forks or
statically links it.

Deployment images (`postgres:16`, `redis:7-alpine`) run beside the
app and are not distributed with it; their own licences
(PostgreSQL, BSD-3-Clause) impose nothing on this codebase.

This is a dependency record, not legal advice. Recheck it when
`requirements.lock` or `package-lock.json` changes.
