-- TP Jewellers chatbot schema
-- Target: PostgreSQL 16+ with the pgvector extension.

CREATE EXTENSION IF NOT EXISTS pgcrypto; -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS vector;   -- pgvector

CREATE TYPE order_status AS ENUM (
  'PLACED', 'CONFIRMED', 'SHIPPED', 'DELIVERED', 'CANCELLED', 'RETURNED'
);

CREATE TYPE message_role AS ENUM ('user', 'assistant', 'system', 'tool');

-- ============================================================
-- Customers
-- ============================================================
CREATE TABLE customers (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name        TEXT NOT NULL,
  email       TEXT NOT NULL UNIQUE,
  phone       TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- Products
-- ============================================================
CREATE TABLE products (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  sku               TEXT NOT NULL UNIQUE,
  name              TEXT NOT NULL,
  category          TEXT NOT NULL,           -- ring | necklace | earring | bracelet | bangle | pendant
  description       TEXT,
  price_cents       BIGINT NOT NULL,
  metal             TEXT,                    -- gold | silver | platinum | rose_gold
  stone             TEXT,                    -- diamond | ruby | emerald | sapphire | none
  sizes_available   JSONB,                   -- e.g. ["5","6","7","8"], null for non-sized items
  stock_by_size     JSONB NOT NULL DEFAULT '{}'::jsonb,  -- e.g. {"6": 3, "7": 0}; {"_default": 12} for unsized
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- Orders
-- ============================================================
CREATE TABLE orders (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  order_number        TEXT NOT NULL UNIQUE,  -- human-facing, e.g. "TPJ-10234"
  customer_id         UUID NOT NULL REFERENCES customers(id),
  status              order_status NOT NULL DEFAULT 'PLACED',
  placed_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  shipped_at          TIMESTAMPTZ,
  delivered_at        TIMESTAMPTZ,
  total_amount_cents  BIGINT NOT NULL,
  shipping_address    JSONB,
  payment_status      TEXT NOT NULL DEFAULT 'PAID',  -- PAID | REFUNDED | PENDING | refund_pending
  -- Set when this order was placed using coupon balance (src/tools/purchase_tools.py).
  -- discount_cents is tracked separately from total_amount_cents so the "real" catalog
  -- total stays visible/auditable alongside what the coupon actually covered.
  -- FKs to coupons(id) / gold_sip_subscriptions(id) added below via ALTER TABLE,
  -- once those tables exist — they in turn reference orders(id), so these are
  -- mutually referential and the FKs have to be added after all CREATE TABLEs.
  coupon_id                 UUID,
  gold_sip_subscription_id  UUID,
  discount_cents            BIGINT NOT NULL DEFAULT 0
);

CREATE INDEX orders_customer_id_idx ON orders(customer_id);

CREATE TABLE order_items (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  order_id          UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  product_id        UUID NOT NULL REFERENCES products(id),
  quantity          INT NOT NULL DEFAULT 1,
  unit_price_cents  BIGINT NOT NULL,
  size              TEXT
);

CREATE INDEX order_items_order_id_idx ON order_items(order_id);

CREATE TABLE order_status_history (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  order_id      UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  from_status   order_status,
  to_status     order_status NOT NULL,
  changed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  reason        TEXT
);

CREATE INDEX order_status_history_order_id_idx ON order_status_history(order_id);

-- ============================================================
-- Knowledge base (hybrid search: full-text + vector)
-- ============================================================
-- Embedding dimension must match whatever embedding model is actually used
-- (1024 matches Voyage AI's voyage-3 family — adjust if you pick a different model).
CREATE TABLE knowledge_docs (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  title         TEXT NOT NULL,
  content       TEXT NOT NULL,
  category      TEXT NOT NULL,   -- policy | care_guide | faq | sizing_guide
  content_tsv   TSVECTOR GENERATED ALWAYS AS (
                  to_tsvector('english', coalesce(title, '') || ' ' || coalesce(content, ''))
                ) STORED,
  embedding     VECTOR(1024),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX knowledge_docs_tsv_idx ON knowledge_docs USING GIN(content_tsv);
-- ivfflat requires ANALYZE + a non-trivial row count to be effective; fine to defer
-- creating this index until the knowledge base has real volume. Left here for when it does:
-- CREATE INDEX knowledge_docs_embedding_idx ON knowledge_docs
--   USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ============================================================
-- Conversation state
-- ============================================================
CREATE TABLE conversations (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id   UUID REFERENCES customers(id),  -- nullable: pre-authentication turns
  started_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE messages (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id   UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role              message_role NOT NULL,
  content           TEXT NOT NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX messages_conversation_id_idx ON messages(conversation_id);

-- Every tool call the agent makes, regardless of outcome — the audit trail.
CREATE TABLE tool_call_log (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id   UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  tool_name         TEXT NOT NULL,
  arguments         JSONB NOT NULL,
  result            JSONB,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX tool_call_log_conversation_id_idx ON tool_call_log(conversation_id);

-- Guardrail: a mutating action (e.g. cancel_order) requires a token minted by an
-- explicit prior confirmation step; the token is single-use and short-lived.
CREATE TABLE confirmation_tokens (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id   UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  action            TEXT NOT NULL,   -- e.g. 'cancel_order'
  target_id         UUID NOT NULL,   -- e.g. the order id
  token             TEXT NOT NULL UNIQUE,
  expires_at        TIMESTAMPTZ NOT NULL,
  used_at           TIMESTAMPTZ,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- Coupons — instant store-credit alternative to a cash refund on
-- cancellation/return. See src/tools/coupon_tools.py and JOURNAL.md for the
-- business rationale and the security guardrails around this table.
-- ============================================================

-- Team-editable incentive policy. No code deploy needed to tune the bonus —
-- just UPDATE this row. bonus_percent_applied on each coupon snapshots the
-- policy at issuance time so later edits don't retroactively change coupons
-- already given out.
CREATE TABLE coupon_policy (
  id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name                        TEXT NOT NULL UNIQUE DEFAULT 'default',
  cancellation_bonus_percent  NUMERIC NOT NULL DEFAULT 10,
  return_bonus_percent        NUMERIC NOT NULL DEFAULT 15,
  expiry_days                 INT NOT NULL DEFAULT 180,
  updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- source_order_id / source_subscription_id are mutually exclusive — a coupon
-- comes from EITHER a cancelled/returned order OR an early-exited Gold SIP
-- (src/tools/gold_sip_tools.py), never both. source_subscription_id's FK to
-- gold_sip_subscriptions(id) is added later via ALTER TABLE, since that table
-- doesn't exist yet at this point in the file (coupons and gold_sip_subscriptions
-- are mutually referential — coupons.source_subscription_id / gold_sip_subscriptions.exit_coupon_id).
CREATE TABLE coupons (
  id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  code                   TEXT NOT NULL UNIQUE,   -- e.g. "TPJ-CPN-xxxxxxxxxxxx"
  customer_id            UUID NOT NULL REFERENCES customers(id),
  source_type            TEXT NOT NULL,          -- 'cancellation' | 'return' | 'gold_sip_cancellation'
  source_order_id        UUID REFERENCES orders(id),
  source_subscription_id UUID,
  amount_cents           BIGINT NOT NULL,        -- original order/SIP value the coupon is based on
  bonus_percent_applied  NUMERIC NOT NULL,        -- snapshot of policy at issuance (0 for SIP early-exit)
  total_cents            BIGINT NOT NULL,        -- amount_cents * (1 + bonus_percent_applied/100)
  remaining_cents        BIGINT NOT NULL,        -- decremented atomically as it's spent; starts == total_cents
  status                 TEXT NOT NULL DEFAULT 'ACTIVE',  -- ACTIVE | REDEEMED | EXPIRED | CANCELLED
  issued_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at             TIMESTAMPTZ NOT NULL,
  CHECK (remaining_cents >= 0 AND remaining_cents <= total_cents),
  CHECK (num_nonnulls(source_order_id, source_subscription_id) = 1)
);

-- One coupon per source (order OR subscription) — enforced at the DB level via
-- partial unique indexes (can't be a plain column UNIQUE since the column is
-- nullable and either one might be the null side), not just application logic,
-- so a bug or a retried tool call can't double-issue from the same source.
CREATE UNIQUE INDEX coupons_source_order_id_uidx ON coupons(source_order_id) WHERE source_order_id IS NOT NULL;
CREATE UNIQUE INDEX coupons_source_subscription_id_uidx ON coupons(source_subscription_id) WHERE source_subscription_id IS NOT NULL;

CREATE INDEX coupons_customer_id_idx ON coupons(customer_id);
CREATE INDEX coupons_code_idx ON coupons(code);

ALTER TABLE orders
  ADD CONSTRAINT orders_coupon_id_fkey FOREIGN KEY (coupon_id) REFERENCES coupons(id);

-- ============================================================
-- Gold SIP (Systematic Investment Plan) schemes — see src/tools/gold_sip_tools.py
-- and JOURNAL.md for the business rationale and security guardrails.
-- ============================================================

-- Team-editable plan tiers. Multiple named rows (unlike coupon_policy's single
-- row) since a store typically offers a few tenure/bonus combinations.
CREATE TABLE gold_sip_plans (
  id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name                        TEXT NOT NULL UNIQUE,
  tenure_months               INT NOT NULL,
  bonus_percent               NUMERIC NOT NULL,
  early_exit_penalty_percent  NUMERIC NOT NULL,
  active                      BOOLEAN NOT NULL DEFAULT true,
  created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE gold_sip_subscriptions (
  id                                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  code                                   TEXT NOT NULL UNIQUE,  -- e.g. "TPJ-SIP-xxxxxxxxxxxx", human-referenceable
  customer_id                           UUID NOT NULL REFERENCES customers(id),
  plan_id                               UUID NOT NULL REFERENCES gold_sip_plans(id),
  -- Snapshotted at start time so a later edit to gold_sip_plans doesn't
  -- retroactively change an in-progress subscription's terms.
  tenure_months_snapshot                INT NOT NULL,
  bonus_percent_snapshot                NUMERIC NOT NULL,
  early_exit_penalty_percent_snapshot   NUMERIC NOT NULL,
  monthly_amount_cents                  BIGINT NOT NULL,
  installments_paid                     INT NOT NULL DEFAULT 0,
  total_paid_cents                      BIGINT NOT NULL DEFAULT 0,
  redeemable_cents                      BIGINT,          -- set at maturity
  remaining_cents                       BIGINT,          -- decremented atomically as it's spent
  status                                TEXT NOT NULL DEFAULT 'ACTIVE',  -- ACTIVE | MATURED | REDEEMED | CANCELLED
  started_at                            TIMESTAMPTZ NOT NULL DEFAULT now(),
  matured_at                            TIMESTAMPTZ,
  cancelled_at                          TIMESTAMPTZ,
  exit_coupon_id                        UUID REFERENCES coupons(id),
  CHECK (remaining_cents IS NULL OR (remaining_cents >= 0 AND remaining_cents <= redeemable_cents))
);

CREATE INDEX gold_sip_subscriptions_customer_id_idx ON gold_sip_subscriptions(customer_id);

ALTER TABLE coupons
  ADD CONSTRAINT coupons_source_subscription_id_fkey
  FOREIGN KEY (source_subscription_id) REFERENCES gold_sip_subscriptions(id);

ALTER TABLE orders
  ADD CONSTRAINT orders_gold_sip_subscription_id_fkey
  FOREIGN KEY (gold_sip_subscription_id) REFERENCES gold_sip_subscriptions(id);

-- Deliberately no stacking a coupon AND a Gold SIP redemption on the same
-- order in this scope — enforced at the DB level, not just application logic.
ALTER TABLE orders
  ADD CONSTRAINT orders_single_discount_source_chk
  CHECK (num_nonnulls(coupon_id, gold_sip_subscription_id) <= 1);

-- Ledger of individual installment payments. UNIQUE(subscription_id,
-- installment_number) is the DB-enforced idempotency guard against double-
-- counting the same installment under a concurrent-call race — same pattern
-- as coupons' one-coupon-per-source partial unique indexes above.
CREATE TABLE gold_sip_installments (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  subscription_id     UUID NOT NULL REFERENCES gold_sip_subscriptions(id) ON DELETE CASCADE,
  installment_number  INT NOT NULL,
  amount_cents        BIGINT NOT NULL,
  paid_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (subscription_id, installment_number)
);
