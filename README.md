# OGX Expedition

Expedition tracker, stats dashboard and fleet optimizer for OGame.

Part of the OGX Oracle toolchain. Shares the same Railway Postgres DB.

## Features

- **Import** — Paste expedition messages directly from OGame inbox (DE / EN / FR)
- **Stats** — Resource totals, DM, pirate win rates, vanish rates over time
- **Results** — Per-expedition breakdown with outcome types
- **DM tracker** — Dark Matter history and Schwarzer Horizont bonuses
- **Fleet Optimizer** — Safe / Balanced / Aggressive fleet suggestions based on your loot history. Paste directly from OGame fleet dialog (DE / EN / FR)
- **Smuggler Codes** — Automatically extracted from expedition messages, stored encrypted at rest
- **Oracle link** — Connects to OGX Oracle for galaxy data
- **i18n** — Full DE / EN / FR interface, auto-detected from browser

## Setup on Railway

1. Create a new Service in your existing Railway project (same project as ogx-oracle)
2. Connect this repo
3. Set environment variables:

```
DATABASE_URL          = (same as ogx-oracle — shared DB, separate tables)
EXP_ENV               = prod
EXP_SECRET_KEY        = <random 32+ chars>
EXP_JWT_SECRET        = <same as OGX_JWT_SECRET in ogx-oracle>
EXP_ALLOW_PUBLIC_BIND = 1
CODE_ENCRYPTION_KEY   = <random secret for smuggler code encryption>
```

> **Important:** `EXP_JWT_SECRET` must match `OGX_JWT_SECRET` in ogx-oracle
> so that logins created there work here too (shared `users` table).

> **Important:** `CODE_ENCRYPTION_KEY` encrypts smuggler codes at rest (Fernet AES).
> Never change this after initial setup — existing codes cannot be decrypted without it.

## Local dev

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8001
```

## One-time DB migrations

After first deploy, run these migration scripts once if upgrading from an older version:

```bash
# Encrypt existing plain-text smuggler codes
CODE_ENCRYPTION_KEY=your-key python migrate_encrypt_codes.py "postgresql://..."

# Add code_hash column for dedup (required after encryption)
CODE_ENCRYPTION_KEY=your-key python migrate_add_code_hash.py "postgresql://..."
```

On fresh installs, tables are auto-created on first boot in dev mode.
For prod: set `EXP_ENV=dev` on the first deploy, then switch back to `prod`.

## Supported expedition message formats

The parser handles all OGame server languages automatically:

| Language | Block header | Example outcome |
|----------|-------------|-----------------|
| DE | `Flottenkommando Expeditionsbericht` | `Expedition erfolgreich` |
| EN | `Fleet Command Expedition Report` | `Expedition successful` |
| FR | `Commandement de la flotte Rapport d'expédition` | `Expédition réussie` |

Copy-paste from both tab-separated (desktop) and space-separated (browser HTML) formats are supported.
