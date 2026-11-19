-- Sprinto cold outreach — Postgres schema.
-- Run once:   python3 db.py init
-- Backfill:   python3 db.py backfill
--
-- One row per (date, domain). The full defensibility trace stays as JSONB
-- so nothing is lost; the flat columns are what dashboards and SQL queries
-- actually index on.

CREATE TABLE IF NOT EXISTS runs (
  id               BIGSERIAL PRIMARY KEY,
  run_date         DATE        NOT NULL,
  domain           TEXT        NOT NULL,
  decision         TEXT        NOT NULL,        -- qualify | qualify_and_flag | drop | error
  priority_score   INT,
  reason           TEXT,
  poc_name         TEXT,
  poc_email        TEXT,
  poc_title        TEXT,
  poc_tier         INT,
  poc_confidence   INT,
  email_subject    TEXT,
  email_body       TEXT,
  email_word_count INT,
  email_sent       BOOLEAN     NOT NULL DEFAULT FALSE,
  resend_id        TEXT,
  trace            JSONB       NOT NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (run_date, domain)
);

CREATE INDEX IF NOT EXISTS idx_runs_date     ON runs (run_date DESC);
CREATE INDEX IF NOT EXISTS idx_runs_decision ON runs (decision);
CREATE INDEX IF NOT EXISTS idx_runs_domain   ON runs (domain);
CREATE INDEX IF NOT EXISTS idx_runs_trace    ON runs USING GIN (trace jsonb_path_ops);
