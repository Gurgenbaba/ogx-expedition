# AGENTS.md — OGX Oracle / Expedition Project Rules

This file defines strict working rules for AI coding agents operating in this repository.

The goal is: production-ready, secure, maintainable code for OGX tools.

------------------------------------------------------------
CORE PRINCIPLES
------------------------------------------------------------

- Security first.
- No silent behavior changes.
- No large refactors unless explicitly requested.
- Railway production environment must remain deployable at all times.
- Preserve compatibility with existing database schema.

------------------------------------------------------------
PROJECT CONTEXT
------------------------------------------------------------

Stack:
- Python 3.12
- FastAPI
- SQLAlchemy
- Postgres (Railway)
- Jinja2 templates
- Tampermonkey collector (read-only, no automation)

Deployment:
- Railway (production)
- Environment variables managed via Railway dashboard
- No secrets in repository

------------------------------------------------------------
STRICT RULES
------------------------------------------------------------

1. NEVER introduce automation/cheat logic for OGame/OGX.
2. NEVER bypass rate limiting or auth.
3. NEVER log secrets or tokens.
4. NEVER modify DB schema without explicit migration plan.
5. NEVER remove security middleware without explanation.

------------------------------------------------------------
CODE CHANGE FORMAT (MANDATORY)
------------------------------------------------------------

When proposing changes, always respond with:

1. Summary (what & why)
2. Files changed (exact paths)
3. Full copy-paste ready code blocks
4. How to verify (commands + expected result)
5. Rollback instructions

No partial snippets unless explicitly requested.

------------------------------------------------------------
DATABASE RULES
------------------------------------------------------------

- Always use parameterized queries / ORM models.
- No raw SQL unless justified.
- Maintain existing constraints.
- Provide migration strategy before schema change.

------------------------------------------------------------
SECURITY REQUIREMENTS
------------------------------------------------------------

- Validate all user input.
- Escape all template variables.
- Preserve CSRF protection.
- Maintain security headers.
- Do not weaken CSP unless justified.
- Respect existing rate limiting.

------------------------------------------------------------
UI RULES
------------------------------------------------------------

- Follow existing OGX dark theme styling.
- No inline styles.
- Reuse existing CSS tokens.
- Preserve responsiveness.
- Maintain accessibility basics (ARIA where needed).

------------------------------------------------------------
TESTING / VERIFICATION
------------------------------------------------------------

- Provide manual verification steps.
- If logic changes, explain edge cases.
- If security relevant, explain attack surface impact.

------------------------------------------------------------
PERFORMANCE
------------------------------------------------------------

- Avoid N+1 queries.
- Avoid blocking calls in async routes.
- Keep collector endpoints lightweight.

------------------------------------------------------------
OUTPUT DISCIPLINE
------------------------------------------------------------

Be structured.
Be explicit.
Do not assume hidden context.
Do not hallucinate files that do not exist.

------------------------------------------------------------
END
------------------------------------------------------------
