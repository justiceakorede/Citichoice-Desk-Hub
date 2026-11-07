# Citichoice Desk Hub

The backend that runs Citichoice Desk: tenants, plans, wallets, usage pricing and Chatwoot provisioning. FastAPI, PostgreSQL, Alembic.
Staff sign in with Zitadel; WiseCP and n8n talk to it through scoped API keys.

| Read this | When |
|---|---|
| `docs/SETUP.md` | first-time setup and deployment, step by step |
| `docs/OPERATIONS.md` | running it: checks, backups, upgrades, incidents |
| `docs/ZITADEL_LIVE_CHECKLIST.md` | proving staff sign-in works against your real Zitadel |
| `docs/WISECP_AUDIT.md` | what we verified about WiseCP payments and currencies, and the top-up contract |
| `docs/DECISIONS.md` | why things are the way they are |
| `docs/IMPLEMENTATION_STATUS.md` | what is built, what is tested, what is NOT done |

Quick start for developers: `pip install -r requirements-dev.txt && python -m pytest` (SQLite), and with PostgreSQL:
`TEST_DATABASE_URL=postgresql+psycopg://... python -m pytest`. Command-line tools: `python -m app.cli --help`.
