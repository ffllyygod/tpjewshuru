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
-- `role` is the ONLY source of truth for staff access. Admins live in this
-- table (not a separate staff table) deliberately: it means an admin logs in
-- through the exact same OTP flow, and src/api/auth.py's anti-enumeration gate
-- (which checks `customers` before sending a code) keeps working untouched.
-- A separate staff table would have locked staff out of login entirely.
CREATE TABLE customers (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name        TEXT NOT NULL,
  email       TEXT NOT NULL UNIQUE,
  phone       TEXT,
  role        TEXT NOT NULL DEFAULT 'customer' CHECK (role IN ('customer', 'admin')),
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
  -- Nullable on purpose: margin is unknowable for rows without it, and a report
  -- that says "cost data incomplete" is better than one that quietly reports
  -- 100% margin. Only the admin persona ever reads this.
  cost_price_cents     BIGINT,
  low_stock_threshold  INT NOT NULL DEFAULT 2,
  -- Discontinuing a product can't be a DELETE (order_items FKs it), so soft-delete
  -- is the only correct shape. search_products filters on this.
  active               BOOLEAN NOT NULL DEFAULT true,
  -- Final-sale items. The seeded Return Policy doc says custom-sized rings and
  -- engraved pieces cannot be returned unless defective; without this column the
  -- assistant would happily accept a return that its own quoted policy forbids.
  returnable           BOOLEAN NOT NULL DEFAULT true,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
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
  -- CHECK-constrained and uppercase-only. Before this, 'PAID' and 'paid' both
  -- existed in the live DB (add_demo_user.py wrote the lowercase one), which
  -- would silently drop rows from any admin revenue report filtering on 'PAID'.
  -- The constraint is what stops that class of bug coming back.
  payment_status      TEXT NOT NULL DEFAULT 'PAID'
                      CHECK (payment_status IN ('PAID', 'REFUNDED', 'PENDING', 'REFUND_PENDING')),
  -- Set when this order was placed using coupon balance (src/tools/purchase_tools.py).
  -- discount_cents is tracked separately from total_amount_cents so the "real" catalog
  -- total stays visible/auditable alongside what the coupon actually covered.
  -- FKs to coupons(id) / gold_sip_subscriptions(id) added below via ALTER TABLE,
  -- once those tables exist — they in turn reference orders(id), so these are
  -- mutually referential and the FKs have to be added after all CREATE TABLEs.
  coupon_id                 UUID,
  gold_sip_subscription_id  UUID,
  discount_cents            BIGINT NOT NULL DEFAULT 0,
  -- Why this order ended early. Split into a CODE and a free-text NOTE on
  -- purpose: the code is what makes "why are people cancelling" a groupable
  -- question, and free text alone would leave that answerable only by reading
  -- every row. The note carries what the code can't ("wanted 16 not 18").
  -- Nullable because most orders are neither cancelled nor returned, and
  -- because orders resolved before this existed genuinely have no recorded
  -- reason — reporting those as "Not recorded" is honest, inventing a reason
  -- for them would not be. FKs added below, once resolution_reasons exists.
  cancellation_reason_code  TEXT,
  cancellation_reason_note  TEXT,
  return_reason_code        TEXT,
  return_reason_note        TEXT,
  -- Mirrors shipped_at/delivered_at. order_status_history could derive this,
  -- but every return report would then need a join to answer "when".
  returned_at               TIMESTAMPTZ
);

CREATE INDEX orders_customer_id_idx ON orders(customer_id);
-- Every admin report is a date-range scan over orders. Cheap now, load-bearing
-- at real volume.
CREATE INDEX orders_placed_at_idx ON orders(placed_at DESC);

CREATE TABLE order_items (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  order_id          UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  product_id        UUID NOT NULL REFERENCES products(id),
  quantity          INT NOT NULL DEFAULT 1,
  unit_price_cents  BIGINT NOT NULL,
  size              TEXT
);

CREATE INDEX order_items_order_id_idx ON order_items(order_id);
-- Top-seller / revenue-by-category reports all join order_items by product_id.
CREATE INDEX order_items_product_id_idx ON order_items(product_id);

-- Team-editable pick-list for why an order ended early, same principle as
-- coupon_policy: changing the options a customer is offered shouldn't need a
-- code deploy.
--
-- Two independent axes, deliberately not collapsed into one:
--   kind       — WHAT happened (cancelled before dispatch vs returned after
--                delivery). The vocabularies barely overlap: "delivery is
--                taking too long" can only be a cancellation, "doesn't fit"
--                can only be a return.
--   applies_to — WHO may say it. A customer must never be offered "suspected
--                fraudulent order", and staff shouldn't wade through
--                customer-voice options to find theirs.
--
-- requires_note marks the ones that are useless alone: 'other' recorded with no
-- note says exactly as much as no reason at all.
CREATE TABLE resolution_reasons (
  code           TEXT PRIMARY KEY,
  kind           TEXT NOT NULL CHECK (kind IN ('cancellation', 'return')),
  label          TEXT NOT NULL,
  applies_to     TEXT NOT NULL DEFAULT 'both' CHECK (applies_to IN ('customer', 'staff', 'both')),
  requires_note  BOOLEAN NOT NULL DEFAULT false,
  active         BOOLEAN NOT NULL DEFAULT true,
  sort_order     INT NOT NULL DEFAULT 100
);

ALTER TABLE orders
  ADD CONSTRAINT orders_cancellation_reason_code_fkey
    FOREIGN KEY (cancellation_reason_code) REFERENCES resolution_reasons(code),
  ADD CONSTRAINT orders_return_reason_code_fkey
    FOREIGN KEY (return_reason_code) REFERENCES resolution_reasons(code);

-- A resolved order must say why, and must not claim the other kind's reason.
-- Enforced in the DB rather than in application code so that no future code
-- path can write a reasonless cancellation or return — the same principle as
-- coupons_source_matches_type. Rows resolved before reasons existed are
-- backfilled to the inactive 'unspecified' codes by the migration script,
-- BEFORE these constraints are added; otherwise they fail to validate.
ALTER TABLE orders
  ADD CONSTRAINT orders_cancellation_reason_required CHECK (
    (status = 'CANCELLED' AND cancellation_reason_code IS NOT NULL)
    OR (status <> 'CANCELLED' AND cancellation_reason_code IS NULL)
  ),
  ADD CONSTRAINT orders_return_reason_required CHECK (
    (status = 'RETURNED' AND return_reason_code IS NOT NULL)
    OR (status <> 'RETURNED' AND return_reason_code IS NULL)
  );

CREATE INDEX orders_cancellation_reason_idx
  ON orders(cancellation_reason_code) WHERE cancellation_reason_code IS NOT NULL;
CREATE INDEX orders_return_reason_idx
  ON orders(return_reason_code) WHERE return_reason_code IS NOT NULL;

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
-- `mode` decides which toolset and which system prompt this conversation gets.
-- Separate from customers.role on purpose: role says what you MAY do, mode says
-- what you're doing RIGHT NOW. An admin shopping for themselves starts a normal
-- 'customer' conversation and gets exactly the customer experience — so the model
-- is never left choosing between cancel_order and admin_cancel_order on vibes.
-- Setting mode='admin' requires role='admin'; it's fixed for the conversation's
-- lifetime and re-verified on every turn (so a demoted admin loses access at once).
CREATE TABLE conversations (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id   UUID REFERENCES customers(id),  -- nullable: pre-authentication turns
  mode          TEXT NOT NULL DEFAULT 'customer' CHECK (mode IN ('customer', 'admin')),
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
-- Admin writes reuse this table with their own action values
-- ('admin_cancel_order', 'admin_adjust_stock', ...). Note target_id is NOT NULL,
-- which structurally forbids bulk admin writes — one row per confirmation.
CREATE TABLE confirmation_tokens (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id   UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  action            TEXT NOT NULL,   -- e.g. 'cancel_order'
  target_id         UUID NOT NULL,   -- e.g. the order id
  -- The rest of what was previewed, when the target alone doesn't pin the
  -- action down. Cancelling order X is fully described by X; adjusting stock or
  -- issuing a coupon is not — "give this customer ₹500" and "give this customer
  -- ₹5,00,000" share a target_id, so without this a token minted for the small
  -- one would authorise the large one. Admin writes re-derive their effect from
  -- here and reject a call whose arguments drifted from the preview.
  params            JSONB NOT NULL DEFAULT '{}'::jsonb,
  token             TEXT NOT NULL UNIQUE,
  expires_at        TIMESTAMPTZ NOT NULL,
  used_at           TIMESTAMPTZ,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Admin write ledger. NOT redundant with tool_call_log: that table records the
-- MODEL-supplied arguments only, so the server-injected actor identity never
-- appears in it — and "which admin did this to whom" is precisely the question
-- you need answered after a cross-customer write. before_state is what makes a
-- manual reversal possible; `reason` is NOT NULL so no audit row is meaningless.
CREATE TABLE admin_action_log (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  actor_customer_id  UUID NOT NULL REFERENCES customers(id),
  conversation_id    UUID REFERENCES conversations(id) ON DELETE SET NULL,
  action             TEXT NOT NULL,     -- 'admin_cancel_order' | 'admin_adjust_stock' | ...
  target_table       TEXT NOT NULL,     -- 'orders' | 'products' | 'customers'
  target_id          UUID NOT NULL,
  before_state       JSONB,
  after_state        JSONB,
  reason             TEXT NOT NULL,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX admin_action_log_target_idx ON admin_action_log(target_table, target_id);
CREATE INDEX admin_action_log_actor_idx ON admin_action_log(actor_customer_id, created_at DESC);

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
-- (src/tools/gold_sip_tools.py), never both — OR, for a 'goodwill' coupon
-- issued by staff, from neither. source_subscription_id's FK to
-- gold_sip_subscriptions(id) is added later via ALTER TABLE, since that table
-- doesn't exist yet at this point in the file (coupons and gold_sip_subscriptions
-- are mutually referential — coupons.source_subscription_id / gold_sip_subscriptions.exit_coupon_id).
CREATE TABLE coupons (
  id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  code                   TEXT NOT NULL UNIQUE,   -- e.g. "TPJ-CPN-xxxxxxxxxxxx"
  customer_id            UUID NOT NULL REFERENCES customers(id),
  source_type            TEXT NOT NULL,          -- 'cancellation' | 'return' | 'gold_sip_cancellation' | 'goodwill'
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
  -- Was `= 1` — every coupon had to trace back to an order or a SIP, which made
  -- a staff goodwill coupon (compensation for a delayed delivery, say)
  -- structurally impossible to represent. Relaxing it to `<= 1` alone would
  -- have been too loose: it would also permit a *cancellation* coupon with no
  -- order behind it, quietly detaching the audit trail that the settlement flow
  -- depends on. So the sourceless case is admitted only for source_type
  -- 'goodwill', which by definition has no source but must instead have an
  -- admin_action_log row naming the staff member who issued it.
  CONSTRAINT coupons_source_matches_type CHECK (
    CASE WHEN source_type = 'goodwill'
         THEN num_nonnulls(source_order_id, source_subscription_id) = 0
         ELSE num_nonnulls(source_order_id, source_subscription_id) = 1
    END
  )
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
