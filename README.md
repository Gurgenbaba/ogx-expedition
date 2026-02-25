# OGX Expedition

Expedition tracker, stats dashboard and fleet optimizer for OGame.

Part of the OGX Oracle toolchain. Shares the same Railway Postgres DB.

## Setup on Railway

1. Create a new Service in your existing Railway project (same project as ogx-oraclev2)
2. Connect this repo
3. Set environment variables:

```
DATABASE_URL        = (same as ogx-oraclev2 — shared DB, new tables)
EXP_ENV             = prod
EXP_SECRET_KEY      = <random 32+ chars>
EXP_JWT_SECRET      = <same as OGX_JWT_SECRET in ogx-oraclev2>
EXP_ALLOW_PUBLIC_BIND = 1
```

> **Important:** `EXP_JWT_SECRET` must match `OGX_JWT_SECRET` in ogx-oraclev2
> so that logins created there work here too (shared `users` table).

## Local dev

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8001
```

## Migrations

On first boot (dev): tables are auto-created via SQLAlchemy.
On prod: run once manually or via Railway one-off:
```bash
python -c "import asyncio; from app.db import engine; from app.models import Base; asyncio.run(engine.begin().__aenter__())"
```
Or just set `EXP_ENV=dev` on the first deploy, then switch back to `prod`.
