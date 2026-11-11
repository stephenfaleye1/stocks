# quant-arena: autonomous quant research & paper-execution workspace

**Status: Phase 1 (Plumbing & Workspace Isolation) is scaffolded. Paused for verification on the workstation before Phase 2.**

Built from *Autonomous Quant Trading Architecture & Claude Code Prompt*. Design rule: **LLMs do research only. Pricing, Greeks, sizing, stops, and order routing are deterministic Python.** The broker connection defaults to **IBKR paper**.

## Folder tree

Items marked `(P2)`, `(P3)`, or `(P4)` come in later phases and are not created yet.

```
quant-arena/
├── docker-compose.quant.yml     # isolated stack on the `quant-net` network
├── .env.example                 # copy to .env (git-ignored)
├── pyproject.toml
├── config/
│   └── risk_rules.yaml          # Risk Governor limits; validated at startup
├── db/
│   ├── migrations/001_init.sql  # quant_trading schema
│   └── scripts/apply_migrations.sh
├── docker/Dockerfile.tools      # Python image for verify/one-shot tools
├── src/quant_common/
│   ├── settings.py              # env → typed settings
│   ├── safety.py                # paper/live interlocks (pure, unit-tested)
│   ├── risk_rules.py            # strict Pydantic model of risk_rules.yaml
│   └── notify.py                # Discord / Telegram alerts
├── scripts/phase1_verify.py     # the Phase 1 verification gate
├── tests/                       # pytest: safety interlocks + risk-rule guardrails
├── services/quant-spine/        # (P2) OpenBB/Polygon data, regime classifier, scanners, VectorBT + Monte Carlo
├── services/alpha-committee/    # (P3) Bull/Bear debate via LiteLLM, EDGAR ingestion, Qdrant, TradeTicket schema
└── services/execution-governor/ # (P4) Risk Governor, ATR stops, Profit Ratchet, IBKR router
```

## How it attaches to the existing workstation

| Existing service | How quant-arena reaches it | What it touches |
|---|---|---|
| PostgreSQL | `host.docker.internal:5432` | A **new** `quant_trading` database owned by a **new** `quant_app` role. Admin credentials are used only by the one-shot migrator. |
| LiteLLM | `host.docker.internal:4000` | Uses one virtual key and a model alias (`LITELLM_MODEL_RESEARCH`). |
| Qdrant | `host.docker.internal:6333` | Only the `quant_`-prefixed collections. |
| IB Gateway | New container `quant-ib-gateway` on `quant-net` | Paper login only. The gateway's `TRADING_MODE: paper` and `READ_ONLY_API: yes` are hard-coded in the compose file, not read from `.env`. |

Your other compose projects are not modified. Every host port binding is `127.0.0.1`-only. If you'd rather join your AI stack's Docker network than go through `host.docker.internal`, add it as an `external: true` network and change the hostnames in `.env`.

## Schema (`quant_trading`)

These are the seven requested tables, plus three supporting ones:

- `market_regimes`: one pre-market classification per day (VIX term structure, breadth, macro events).
- `scanner_candidates`: deterministic screener output, ranked. Unique per (date, scanner, symbol).
- `agent_debates`: Bull/Bear transcripts, red flags, sources (EDGAR accession numbers), Qdrant point IDs, and token usage.
- `trade_tickets`: Pydantic-validated tickets. `ticket_hash` is unique, which dedupes tickets. The governor stores its rule-by-rule decision here.
- `positions`: legs, defined `max_loss_usd` (required to be > 0), and ATR trailing-stop state (ATR, high watermark, stop price).
- `orders` (added): `client_order_id UNIQUE`. This is the database-level guard behind the Phase 4 gate of "zero duplicate orders".
- `execution_logs`: an append-only audit trail. A trigger rejects UPDATE and DELETE.
- `equity_snapshots` (added): NAV, reserve, and peak drawdown, used by the circuit breakers.
- `equity_ratchet`: append-only. Each milestone can lock profit only once per account.
- `schema_migrations`: filename plus sha256. The migrator refuses to run if an applied migration file was edited.

## Safety interlocks

1. The IB Gateway container is hard-coded to `TRADING_MODE: paper` and `READ_ONLY_API: yes`.
2. `safety.assert_broker_target_safe` checks the port. Paper mode rejects the live ports (4001, 4003, 7496) and any unknown port. Live mode needs four things at once: `TRADING_MODE=live`, `LIVE_TRADING_ENABLED=true`, the exact confirmation phrase, **and** a code path that passes `allow_live=True`. No code path does that today.
3. After connecting, `assert_accounts_match_mode` requires every managed account to start with `DU` (the IBKR paper prefix).
4. `risk_rules.yaml` is validated by a strict model. Typos and unknown keys are rejected. The model also enforces the mandate ceilings, so these settings can't be loosened by editing YAML: naked short options, margin, short equity, more than 25% per position, more than 4 positions, and turning off the upward-only trailing stop or idempotency.
5. A `KILL_SWITCH` env flag is reserved for the Phase 4 governor.

## Phase 1 runbook (Windows 11 / Docker Desktop, PowerShell)

```powershell
cd C:\Workspaces\quant-arena
Copy-Item .env.example .env    # then edit .env: paper IBKR login, PG admin + app passwords, LiteLLM key/alias

# 1. Create quant_trading and apply the schema (idempotent; safe to re-run)
docker compose -f docker-compose.quant.yml --profile tools run --rm quant-db-migrate

# 2. Start the paper IB Gateway (first login may need IBKR Mobile 2FA approval)
docker compose -f docker-compose.quant.yml up -d ib-gateway
docker compose -f docker-compose.quant.yml logs -f ib-gateway   # wait for "Login has completed"

# 3. Run the verification gate (also creates the Qdrant collections)
docker compose -f docker-compose.quant.yml --profile tools run --rm --build quant-verify `
  python -m scripts.phase1_verify --init-qdrant
```

Expected output:

```
  [PASS] config    rules v1: 4 pos max, 25%/pos, paper port 4004
  [PASS] postgres  PG 16.x, 10 tables present, 1 migration(s) applied
  [PASS] litellm   alias 'research-default' -> <model> replied 'PONG' in 0.8s
  [PASS] qdrant    reachable; 3/3 project collections (created ...)
  [PASS] ibkr      connected ib-gateway:4004 read-only, accounts=['DU…'], server time ...
  [PASS] webhook   delivered to discord
GATE: PASSED
```

**Phase 1 gate:** a paper API heartbeat from IBKR, the Postgres tables verified, and LiteLLM routing confirmed. Webhook delivery is optional.

Before running, check two things:

- Set `QDRANT_VECTOR_SIZE` to the dimension of the embedding model you plan to use in Phase 3. Qdrant can't change a collection's dimension later; you'd have to delete and recreate it.
- Pin `IB_GATEWAY_IMAGE` to a specific tag once it works, instead of `stable`.

## Development

```bash
pip install -e '.[dev]'
pytest -q
```

## Host hardening checklist (from the blueprint)

- [ ] Power plan set to High Performance, sleep set to Never
- [ ] Windows Update active hours set outside 4:00 AM to 8:00 PM ET
- [ ] UPS connected; Docker Desktop set to start at login
- [ ] Discord or Telegram webhook configured in `.env`
