-- =============================================================================
-- quant_trading — 001_init
-- Core state for regime -> scanner -> committee -> governor -> broker pipeline.
-- Enumerations are CHECK constraints (easy to extend in later migrations).
-- Money is NUMERIC; timestamps are TIMESTAMPTZ (UTC).
-- =============================================================================

-- gen_random_uuid() is built in from PostgreSQL 13+; no extension required.

-- Shared updated_at trigger ----------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END $$ LANGUAGE plpgsql;

-- 1. market_regimes: one pre-market classification per trading day -----------
CREATE TABLE market_regimes (
  id               BIGSERIAL PRIMARY KEY,
  trade_date       DATE        NOT NULL UNIQUE,
  regime           TEXT        NOT NULL
                   CHECK (regime IN ('trend_momentum','high_vol_catalyst','chop_defensive')),
  confidence       NUMERIC(5,4) CHECK (confidence BETWEEN 0 AND 1),
  vix_spot         NUMERIC(8,3),
  vix_3m           NUMERIC(8,3),
  vix_term_ratio   NUMERIC(8,4),            -- VIX / VIX3M (>1 = backwardation)
  breadth_pct_above_50dma NUMERIC(5,4),
  advance_decline  NUMERIC(10,4),
  macro_events     JSONB       NOT NULL DEFAULT '[]'::jsonb,
  features         JSONB       NOT NULL DEFAULT '{}'::jsonb,
  classifier_version TEXT      NOT NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2. scanner_candidates: deterministic screener output -----------------------
CREATE TABLE scanner_candidates (
  id               BIGSERIAL PRIMARY KEY,
  trade_date       DATE        NOT NULL,
  regime_id        BIGINT      REFERENCES market_regimes(id),
  scanner          TEXT        NOT NULL
                   CHECK (scanner IN ('momentum_equity','catalyst_options')),
  symbol           TEXT        NOT NULL CHECK (symbol ~ '^[A-Z][A-Z0-9.\-]{0,9}$'),
  rank             INTEGER     NOT NULL CHECK (rank > 0),
  score            NUMERIC(12,6) NOT NULL,
  last_price       NUMERIC(14,4) NOT NULL CHECK (last_price > 0),
  relative_volume  NUMERIC(10,4),
  atr_14           NUMERIC(14,4),
  iv_rank          NUMERIC(6,4),
  payoff_asymmetry NUMERIC(10,4),           -- max_gain / max_loss for best structure
  proposed_structure JSONB,                 -- deterministic legs, priced from market data
  metrics          JSONB       NOT NULL DEFAULT '{}'::jsonb,
  data_source      TEXT        NOT NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (trade_date, scanner, symbol)
);
CREATE INDEX scanner_candidates_date_rank ON scanner_candidates (trade_date, scanner, rank);

-- 3. agent_debates: LLM Bull/Bear committee transcripts -----------------------
CREATE TABLE agent_debates (
  id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  candidate_id     BIGINT      NOT NULL REFERENCES scanner_candidates(id),
  symbol           TEXT        NOT NULL,
  verdict          TEXT        NOT NULL
                   CHECK (verdict IN ('approve','reject','watchlist','error')),
  conviction       NUMERIC(5,4) CHECK (conviction BETWEEN 0 AND 1),
  bull_case        TEXT,
  bear_case        TEXT,
  red_flags        JSONB       NOT NULL DEFAULT '[]'::jsonb,
  sources          JSONB       NOT NULL DEFAULT '[]'::jsonb,  -- EDGAR accession nos., URLs
  qdrant_point_ids JSONB       NOT NULL DEFAULT '[]'::jsonb,
  models_used      JSONB       NOT NULL DEFAULT '{}'::jsonb,  -- LiteLLM aliases per role
  token_usage      JSONB       NOT NULL DEFAULT '{}'::jsonb,
  transcript       JSONB       NOT NULL DEFAULT '[]'::jsonb,
  started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at      TIMESTAMPTZ
);
CREATE INDEX agent_debates_candidate ON agent_debates (candidate_id);

-- 4. trade_tickets: schema-validated proposals awaiting the Risk Governor -----
CREATE TABLE trade_tickets (
  id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  debate_id        UUID        REFERENCES agent_debates(id),
  candidate_id     BIGINT      REFERENCES scanner_candidates(id),
  symbol           TEXT        NOT NULL,
  structure        TEXT        NOT NULL
                   CHECK (structure IN ('long_call','long_put','call_debit_spread',
                                        'put_debit_spread','long_straddle','long_strangle',
                                        'leaps_call','long_equity')),
  ticket           JSONB       NOT NULL,      -- full Pydantic TradeTicket payload
  ticket_schema_version TEXT   NOT NULL,
  ticket_hash      TEXT        NOT NULL UNIQUE,  -- sha256 of canonical ticket; dedupe
  status           TEXT        NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','approved','rejected','submitted',
                                     'filled','partially_filled','cancelled','expired')),
  governor_decision JSONB,                    -- every rule evaluated + pass/fail reason
  governor_decided_at TIMESTAMPTZ,
  expires_at       TIMESTAMPTZ NOT NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX trade_tickets_status ON trade_tickets (status, created_at);
CREATE TRIGGER trade_tickets_updated BEFORE UPDATE ON trade_tickets
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- 5. positions: open/closed positions incl. ATR trailing-stop watermarks ------
CREATE TABLE positions (
  id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  ticket_id        UUID        REFERENCES trade_tickets(id),
  trading_mode     TEXT        NOT NULL CHECK (trading_mode IN ('paper','live')),
  ib_account       TEXT        NOT NULL,
  symbol           TEXT        NOT NULL,
  structure        TEXT        NOT NULL,
  legs             JSONB       NOT NULL,      -- [{conId, right, strike, expiry, ratio, action}]
  quantity         NUMERIC(14,4) NOT NULL CHECK (quantity > 0),
  avg_entry_price  NUMERIC(14,4) NOT NULL,
  cost_basis_usd   NUMERIC(14,2) NOT NULL,
  max_loss_usd     NUMERIC(14,2) NOT NULL CHECK (max_loss_usd > 0),  -- defined risk only
  pyramid_adds     INTEGER     NOT NULL DEFAULT 0 CHECK (pyramid_adds >= 0),
  -- ATR trailing stop state (deterministic; updated by governor only)
  atr_period       INTEGER,
  atr_multiple     NUMERIC(6,3),
  atr_value        NUMERIC(14,4),
  high_watermark   NUMERIC(14,4),
  stop_price       NUMERIC(14,4),
  stop_updated_at  TIMESTAMPTZ,
  status           TEXT        NOT NULL DEFAULT 'open'
                   CHECK (status IN ('opening','open','closing','closed')),
  exit_reason      TEXT CHECK (exit_reason IN ('atr_stop','premium_stop','target',
                                 'expiry_rule','circuit_breaker','manual','governor')),
  realized_pnl_usd NUMERIC(14,2),
  opened_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  closed_at        TIMESTAMPTZ,
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX positions_open ON positions (status) WHERE status <> 'closed';
CREATE TRIGGER positions_updated BEFORE UPDATE ON positions
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- orders: one row per broker order; client_order_id UNIQUE = no duplicates ---
CREATE TABLE orders (
  id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  client_order_id  TEXT        NOT NULL UNIQUE,   -- deterministic idempotency key
  ticket_id        UUID        REFERENCES trade_tickets(id),
  position_id      UUID        REFERENCES positions(id),
  trading_mode     TEXT        NOT NULL CHECK (trading_mode IN ('paper','live')),
  ib_account       TEXT        NOT NULL,
  ib_order_id      BIGINT,
  ib_perm_id       BIGINT,
  intent           TEXT        NOT NULL
                   CHECK (intent IN ('entry','pyramid_add','stop_exit','target_exit',
                                     'expiry_exit','circuit_breaker_exit','manual_exit')),
  order_type       TEXT        NOT NULL,
  limit_price      NUMERIC(14,4),
  quantity         NUMERIC(14,4) NOT NULL CHECK (quantity > 0),
  legs             JSONB       NOT NULL,
  status           TEXT        NOT NULL DEFAULT 'created'
                   CHECK (status IN ('created','submitted','acknowledged','partially_filled',
                                     'filled','cancelled','rejected','error')),
  filled_quantity  NUMERIC(14,4) NOT NULL DEFAULT 0,
  avg_fill_price   NUMERIC(14,4),
  commission_usd   NUMERIC(10,2),
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX orders_status ON orders (status);
CREATE TRIGGER orders_updated BEFORE UPDATE ON orders
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- 6. execution_logs: APPEND-ONLY audit trail ---------------------------------
CREATE TABLE execution_logs (
  id               BIGSERIAL   PRIMARY KEY,
  occurred_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  component        TEXT        NOT NULL,      -- governor | router | stop_engine | ratchet | ...
  level            TEXT        NOT NULL CHECK (level IN ('debug','info','warning','error','critical')),
  event_type       TEXT        NOT NULL,      -- e.g. order_submitted, stop_triggered, breaker_tripped
  trading_mode     TEXT        NOT NULL CHECK (trading_mode IN ('paper','live')),
  order_id         UUID        REFERENCES orders(id),
  ticket_id        UUID        REFERENCES trade_tickets(id),
  position_id      UUID        REFERENCES positions(id),
  message          TEXT        NOT NULL,
  payload          JSONB       NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX execution_logs_time ON execution_logs (occurred_at);
CREATE INDEX execution_logs_event ON execution_logs (event_type, occurred_at);

CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER execution_logs_append_only BEFORE UPDATE OR DELETE ON execution_logs
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- equity_snapshots: periodic NAV for drawdown/circuit breakers ---------------
CREATE TABLE equity_snapshots (
  id               BIGSERIAL   PRIMARY KEY,
  taken_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  trading_mode     TEXT        NOT NULL CHECK (trading_mode IN ('paper','live')),
  ib_account       TEXT        NOT NULL,
  net_liquidation_usd NUMERIC(14,2) NOT NULL,
  cash_usd         NUMERIC(14,2) NOT NULL,
  reserve_locked_usd NUMERIC(14,2) NOT NULL DEFAULT 0,
  tradable_equity_usd NUMERIC(14,2) NOT NULL,
  peak_equity_usd  NUMERIC(14,2) NOT NULL,
  drawdown_pct     NUMERIC(7,5) NOT NULL
);
CREATE INDEX equity_snapshots_time ON equity_snapshots (taken_at);

-- 7. equity_ratchet: profit locks at milestones (append-only) ----------------
CREATE TABLE equity_ratchet (
  id               BIGSERIAL   PRIMARY KEY,
  trading_mode     TEXT        NOT NULL CHECK (trading_mode IN ('paper','live')),
  ib_account       TEXT        NOT NULL,
  milestone_usd    NUMERIC(14,2) NOT NULL,
  crossed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  equity_at_cross_usd NUMERIC(14,2) NOT NULL,
  gains_at_cross_usd  NUMERIC(14,2) NOT NULL,
  lock_pct_of_gains   NUMERIC(5,4) NOT NULL CHECK (lock_pct_of_gains BETWEEN 0 AND 1),
  locked_amount_usd   NUMERIC(14,2) NOT NULL CHECK (locked_amount_usd >= 0),
  cumulative_reserve_usd NUMERIC(14,2) NOT NULL CHECK (cumulative_reserve_usd >= 0),
  rules_version    INTEGER     NOT NULL,
  note             TEXT,
  UNIQUE (trading_mode, ib_account, milestone_usd)   -- each milestone locks once
);
CREATE TRIGGER equity_ratchet_append_only BEFORE UPDATE OR DELETE ON equity_ratchet
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
