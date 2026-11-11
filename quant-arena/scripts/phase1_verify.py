"""Phase 1 verification gate.

Checks (each independent; a summary table is printed and the exit code is
non-zero if any REQUIRED check fails):

  1. config      risk_rules.yaml validates; broker safety flags are paper-safe
  2. postgres    quant_trading reachable as app role; all core tables exist
  3. litellm     gateway liveness + one tiny completion routed to the research alias
  4. qdrant      reachable; `--init-qdrant` creates the project collections
  5. ibkr        paper API heartbeat: connect read-only, server time, DU account(s)
  6. webhook     test alert delivered (optional — skipped if no channel configured)

Usage:  python -m scripts.phase1_verify [--init-qdrant] [--skip ibkr,litellm]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections.abc import Callable

import httpx

from quant_common.notify import send_alert
from quant_common.risk_rules import load_risk_rules
from quant_common.safety import assert_accounts_match_mode, assert_broker_target_safe
from quant_common.settings import Settings, get_settings

REQUIRED_TABLES = {
    "market_regimes", "scanner_candidates", "agent_debates", "trade_tickets",
    "positions", "orders", "execution_logs", "equity_snapshots", "equity_ratchet",
    "schema_migrations",
}
QDRANT_COLLECTIONS = ("catalysts", "regime_playbooks", "postmortems")


class Skip(Exception):
    pass


def check_config(s: Settings, _args) -> str:
    rules = load_risk_rules(s.risk_rules_path)
    assert_broker_target_safe(
        trading_mode=s.trading_mode,
        port=s.ib_port,
        live_trading_enabled=s.live_trading_enabled,
        live_trading_confirmation=s.live_trading_confirmation,
    )
    if s.trading_mode != "paper":
        raise RuntimeError("Phase 1 requires TRADING_MODE=paper")
    pl = rules.position_limits
    return (f"rules v{rules.version}: {pl.max_concurrent_positions} pos max, "
            f"{pl.max_allocation_pct_per_position:.0%}/pos, paper port {s.ib_port}")


def check_postgres(s: Settings, _args) -> str:
    import psycopg

    with psycopg.connect(s.pg_conninfo()) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
        ).fetchall()
        found = {r[0] for r in rows}
        missing = REQUIRED_TABLES - found
        if missing:
            raise RuntimeError(f"missing tables: {sorted(missing)} — run quant-db-migrate")
        n = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
        ver = conn.execute("SHOW server_version").fetchone()[0]
    return f"PG {ver}, {len(REQUIRED_TABLES)} tables present, {n} migration(s) applied"


def check_litellm(s: Settings, _args) -> str:
    headers = {"Authorization": f"Bearer {s.litellm_api_key.get_secret_value()}"}
    base = s.litellm_base_url.rstrip("/")
    with httpx.Client(timeout=60, headers=headers) as c:
        c.get(f"{base}/health/liveliness").raise_for_status()
        t0 = time.perf_counter()
        r = c.post(f"{base}/v1/chat/completions", json={
            "model": s.litellm_model_research,
            "messages": [{"role": "user", "content": "Reply with exactly: PONG"}],
            "max_tokens": 5,
            "temperature": 0,
        })
        r.raise_for_status()
        body = r.json()
    reply = (body["choices"][0]["message"]["content"] or "").strip()
    return (f"alias '{s.litellm_model_research}' -> {body.get('model', '?')} "
            f"replied {reply!r} in {time.perf_counter() - t0:.1f}s")


def check_qdrant(s: Settings, args) -> str:
    key = s.qdrant_api_key.get_secret_value()
    headers = {"api-key": key} if key else {}
    base = s.qdrant_url.rstrip("/")
    with httpx.Client(timeout=15, headers=headers) as c:
        existing = {x["name"] for x in c.get(f"{base}/collections").raise_for_status()
                    .json()["result"]["collections"]}
        wanted = [s.qdrant_collection_prefix + n for n in QDRANT_COLLECTIONS]
        created = []
        if args.init_qdrant:
            for name in wanted:
                if name not in existing:
                    c.put(f"{base}/collections/{name}", json={
                        "vectors": {"size": s.qdrant_vector_size, "distance": "Cosine"},
                    }).raise_for_status()
                    created.append(name)
        present = [n for n in wanted if n in existing or n in created]
    msg = f"reachable; {len(present)}/{len(wanted)} project collections"
    if created:
        msg += f" (created {', '.join(created)})"
    elif len(present) < len(wanted):
        msg += " — run with --init-qdrant to create"
    return msg


def check_ibkr(s: Settings, _args) -> str:
    from ib_async import IB

    assert_broker_target_safe(trading_mode=s.trading_mode, port=s.ib_port)

    async def _run() -> str:
        ib = IB()
        await ib.connectAsync(s.ib_host, s.ib_port, clientId=s.ib_client_id,
                              readonly=True, timeout=20)
        try:
            accounts = ib.managedAccounts()
            assert_accounts_match_mode(accounts, trading_mode=s.trading_mode,
                                       paper_prefix=s.ib_expected_account_prefix)
            server_time = await ib.reqCurrentTimeAsync()
            return (f"connected {s.ib_host}:{s.ib_port} read-only, accounts={accounts}, "
                    f"server time {server_time:%Y-%m-%d %H:%M:%S %Z}")
        finally:
            ib.disconnect()

    return asyncio.run(_run())


def check_webhook(s: Settings, _args) -> str:
    res = send_alert(s, "Phase 1 verification: webhook test ✅")
    if not res:
        raise Skip("no Discord/Telegram channel configured")
    failed = [k for k, ok in res.items() if not ok]
    if failed:
        raise RuntimeError(f"delivery failed: {failed}")
    return f"delivered to {', '.join(res)}"


CHECKS: list[tuple[str, Callable, bool]] = [
    ("config", check_config, True),
    ("postgres", check_postgres, True),
    ("litellm", check_litellm, True),
    ("qdrant", check_qdrant, True),
    ("ibkr", check_ibkr, True),
    ("webhook", check_webhook, False),
]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--init-qdrant", action="store_true", help="create missing Qdrant collections")
    p.add_argument("--skip", default="", help="comma-separated checks to skip")
    args = p.parse_args(argv)
    skip = {x.strip() for x in args.skip.split(",") if x.strip()}

    s = get_settings()
    print(f"quant-arena Phase 1 verification  (TRADING_MODE={s.trading_mode})\n")
    failed_required = False
    for name, fn, required in CHECKS:
        if name in skip:
            status, detail = "SKIP", "skipped by --skip"
        else:
            try:
                status, detail = "PASS", fn(s, args)
            except Skip as exc:
                status, detail = "SKIP", str(exc)
            except Exception as exc:  # noqa: BLE001 — report every failure, keep going
                status, detail = "FAIL", f"{type(exc).__name__}: {exc}"
                failed_required |= required
        print(f"  [{status}] {name:<9} {detail}")

    print("\nGATE:", "NOT PASSED" if failed_required else "PASSED")
    return 1 if failed_required else 0


if __name__ == "__main__":
    sys.exit(main())
