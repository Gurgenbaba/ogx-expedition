# OGX Expedition

Expedition tracker, stats dashboard and fleet optimizer for OGame.

Part of the OGX Oracle toolchain.
Uses the same Railway PostgreSQL database (separate tables).

------------------------------------------------------------

FEATURES

- Import — Paste expedition messages directly from OGame inbox (DE / EN / FR)
- Stats — Resource totals, Dark Matter, pirate win rates, vanish rates
- Results — Per-expedition breakdown with outcome types
- DM Tracker — Dark Matter history and Schwarzer Horizont bonuses
- Fleet Optimizer — Safe / Balanced / Aggressive fleet suggestions
- Smuggler Codes — Extracted automatically, stored encrypted at rest
- Oracle Link — Connects to OGX Oracle for galaxy data
- i18n — Full DE / EN / FR interface (browser auto-detect)

------------------------------------------------------------

SETUP ON RAILWAY

1) Create a new Service inside your existing Railway project
   (same project as ogx-oracle)

2) Connect this repository

3) Set environment variables:

DATABASE_URL          = same as ogx-oracle (shared DB)
EXP_ENV               = prod
EXP_SECRET_KEY        = random 32+ character string
EXP_JWT_SECRET        = same as OGX_JWT_SECRET (from ogx-oracle)
EXP_ALLOW_PUBLIC_BIND = 1
CODE_ENCRYPTION_KEY   = random secret for smuggler code encryption

IMPORTANT

- EXP_JWT_SECRET must match OGX_JWT_SECRET in ogx-oracle
  so logins work across both services (shared users table).

- CODE_ENCRYPTION_KEY encrypts smuggler codes at rest (Fernet AES).
  Never change this after initial setup.
  Without it, existing encrypted codes cannot be decrypted.

------------------------------------------------------------

LOCAL DEVELOPMENT

pip install -r requirements.txt
uvicorn app.main:app --reload --port 8001

------------------------------------------------------------

ONE-TIME DATABASE MIGRATIONS

If upgrading from an older version:

Encrypt existing plain-text smuggler codes:

CODE_ENCRYPTION_KEY=your-key python migrate_encrypt_codes.py "postgresql://..."

Add code_hash column for deduplication:

CODE_ENCRYPTION_KEY=your-key python migrate_add_code_hash.py "postgresql://..."

On fresh installs:
Tables are auto-created in development mode.

For production:
Set EXP_ENV=dev for first deploy.
After initialization, switch back to EXP_ENV=prod.

------------------------------------------------------------

SUPPORTED EXPEDITION MESSAGE FORMATS

The parser automatically detects all OGX(play.OGX) languages:

German:
Header: Flottenkommando Expeditionsbericht
Example: Expedition erfolgreich

English:
Header: Fleet Command Expedition Report
Example: Expedition successful

French:
Header: Commandement de la flotte Rapport d'expédition
Example: Expédition réussie

Both tab-separated (desktop copy) and space-separated
(browser HTML copy) formats are supported.

------------------------------------------------------------

LICENSE

Private / internal use.
