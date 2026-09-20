-- =====================================================================
-- Lead-to-Cash PoC — core schema (001)
-- Design principles:
--   * Every status is a CHECK-constrained text column (easy to evolve, self-documenting).
--   * "Approved = executed" is enforced IN THE DATABASE: quotes must match their approval,
--     orders must match their quote. The app cannot bypass this even by mistake.
--   * Duplicate prevention: idempotency keys, one quote per approval, one order per quote,
--     one outbound invoice per order, one open exception per (entity, type).
--   * audit_log is append-only (UPDATE/DELETE/TRUNCATE are rejected by triggers).
--   * RLS is enabled on every table with no policies, so Supabase's public REST API
--     (anon/authenticated keys) cannot read or write anything. Only the backend's
--     direct Postgres connection (table owner) has access.
-- =====================================================================

create extension if not exists pgcrypto;

create or replace function set_updated_at() returns trigger
language plpgsql as $$
begin
  new.updated_at := now();
  return new;
end $$;

-- ---------------------------------------------------------------------
-- Master data
-- ---------------------------------------------------------------------
create table products (
  id          uuid primary key default gen_random_uuid(),
  sku         text not null unique,
  name        text not null,
  aliases     text[] not null default '{}',
  unit_price  numeric(12,2) not null check (unit_price >= 0),
  currency    char(3) not null default 'USD',
  is_active   boolean not null default true,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

create table customers (
  id          uuid primary key default gen_random_uuid(),
  name        text not null,
  email       text not null,
  company     text,
  phone       text,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);
create unique index customers_email_uq on customers (lower(email));

-- ---------------------------------------------------------------------
-- Inquiry intake
-- ---------------------------------------------------------------------
create table inquiries (
  id               uuid primary key default gen_random_uuid(),
  idempotency_key  text not null unique,           -- duplicate webhook protection
  request_id       text,                            -- correlation id from n8n
  source           text not null default 'webhook'
                   check (source in ('webhook','email','form','api')),
  customer_id      uuid references customers(id),
  customer_name    text,
  customer_email   text,
  raw_message      text not null check (length(raw_message) between 1 and 5000),
  status           text not null default 'received'
                   check (status in ('received','extracting','needs_review','awaiting_approval',
                                     'approved','rejected','quoted','ordered','invoiced',
                                     'failed','cancelled')),
  received_at      timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);
create index inquiries_status_idx on inquiries (status);

-- Every LLM call is recorded: input, raw output, parsed output, validation result.
create table ai_runs (
  id                 uuid primary key default gen_random_uuid(),
  purpose            text not null
                     check (purpose in ('inquiry_extraction','invoice_extraction','exception_explanation')),
  inquiry_id         uuid references inquiries(id),
  invoice_id         uuid,                          -- FK added below (table defined later)
  exception_id       uuid,                          -- FK added below
  attempt            smallint not null default 1 check (attempt >= 1),
  provider           text not null,                 -- 'openai' | 'mock'
  model              text,
  prompt_version     text not null,
  input_text         text,
  raw_output         text,
  parsed_output      jsonb,
  outcome            text not null
                     check (outcome in ('valid','invalid','low_confidence','error','timeout')),
  validation_errors  jsonb not null default '[]',
  confidence         numeric(4,3) check (confidence between 0 and 1),
  latency_ms         integer,
  error_message      text,
  created_at         timestamptz not null default now(),
  check (num_nonnulls(inquiry_id, invoice_id, exception_id) = 1)
);
create index ai_runs_inquiry_idx on ai_runs (inquiry_id);

-- Output of the deterministic rules engine (versioned, with reasons).
create table rule_decisions (
  id              uuid primary key default gen_random_uuid(),
  inquiry_id      uuid not null references inquiries(id),
  ai_run_id       uuid references ai_runs(id),
  rule_version    text not null,
  inputs          jsonb not null,
  outcome         text not null
                  check (outcome in ('auto_approve','salesperson_approval','manager_approval',
                                     'human_review','reject')),
  reasons         jsonb not null default '[]',
  created_at      timestamptz not null default now()
);
create index rule_decisions_inquiry_idx on rule_decisions (inquiry_id);

-- ---------------------------------------------------------------------
-- Approvals: the single source of truth for commercial terms
-- ---------------------------------------------------------------------
create table approvals (
  id                      uuid primary key default gen_random_uuid(),
  inquiry_id              uuid not null references inquiries(id),
  rule_decision_id        uuid references rule_decisions(id),
  required_role           text not null check (required_role in ('system','salesperson','manager')),
  status                  text not null default 'pending'
                          check (status in ('pending','approved','modified','rejected','expired')),
  product_id              uuid not null references products(id),
  quantity                integer not null check (quantity > 0),
  unit_price              numeric(12,2) not null check (unit_price >= 0),
  requested_discount_pct  numeric(5,2) not null check (requested_discount_pct between 0 and 100),
  approved_discount_pct   numeric(5,2) check (approved_discount_pct between 0 and 100),
  terms_hash              text,          -- sha256 of approved terms; carried to quote/order/invoice
  decided_by              text,
  decision_comment        text,
  token_hash              text unique,   -- hash of the one-time approval link token
  expires_at              timestamptz,
  decided_at              timestamptz,
  created_at              timestamptz not null default now(),
  updated_at              timestamptz not null default now(),
  constraint approvals_final_terms_present check (
    status not in ('approved','modified')
    or (approved_discount_pct is not null and terms_hash is not null
        and decided_by is not null and decided_at is not null)
  )
);
-- At most one pending and at most one granted approval per inquiry.
create unique index approvals_one_pending_uq on approvals (inquiry_id) where status = 'pending';
create unique index approvals_one_granted_uq on approvals (inquiry_id) where status in ('approved','modified');

-- A decided approval is frozen: nobody can change it afterwards.
create or replace function approvals_freeze_decided() returns trigger
language plpgsql as $$
begin
  if old.status <> 'pending' then
    raise exception 'approval % is final (status=%) and cannot be changed', old.id, old.status
      using errcode = 'P0001';
  end if;
  return new;
end $$;
create trigger approvals_freeze_decided before update on approvals
  for each row execute function approvals_freeze_decided();

-- ---------------------------------------------------------------------
-- Appointment (mock scheduling)
-- ---------------------------------------------------------------------
create table appointments (
  id               uuid primary key default gen_random_uuid(),
  inquiry_id       uuid not null unique references inquiries(id),   -- one per inquiry
  customer_id      uuid references customers(id),
  requested_text   text,
  scheduled_start  timestamptz not null,
  scheduled_end    timestamptz not null,
  timezone         text not null default 'UTC',
  status           text not null default 'proposed'
                   check (status in ('proposed','confirmed','cancelled','failed')),
  external_ref     text,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  check (scheduled_end > scheduled_start)
);

-- ---------------------------------------------------------------------
-- Quote -> Order -> Invoice
-- ---------------------------------------------------------------------
create sequence quote_number_seq;
create sequence order_number_seq;
create sequence invoice_number_seq;

create table quotes (
  id               uuid primary key default gen_random_uuid(),
  quote_number     text not null unique
                   default 'Q-' || lpad(nextval('quote_number_seq')::text, 6, '0'),
  inquiry_id       uuid not null references inquiries(id),
  approval_id      uuid not null unique references approvals(id),   -- one quote per approval
  product_id       uuid not null references products(id),
  quantity         integer not null check (quantity > 0),
  unit_price       numeric(12,2) not null check (unit_price >= 0),
  discount_pct     numeric(5,2) not null check (discount_pct between 0 and 100),
  subtotal         numeric(14,2) not null,
  discount_amount  numeric(14,2) not null,
  total            numeric(14,2) not null,
  currency         char(3) not null,
  terms_hash       text not null,
  status           text not null default 'issued'
                   check (status in ('issued','accepted','expired','void')),
  valid_until      date,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  constraint quotes_math check (
    subtotal = round(quantity * unit_price, 2)
    and discount_amount = round(subtotal * discount_pct / 100, 2)
    and total = subtotal - discount_amount
  )
);

-- A quote can only be created from a granted approval, with exactly the approved terms.
create or replace function quotes_enforce_approval() returns trigger
language plpgsql as $$
declare a approvals%rowtype;
begin
  select * into a from approvals where id = new.approval_id;
  if a.status not in ('approved','modified') then
    raise exception 'approval % is not granted (status=%)', a.id, a.status using errcode = 'P0001';
  end if;
  if new.inquiry_id   <> a.inquiry_id
     or new.product_id   <> a.product_id
     or new.quantity     <> a.quantity
     or new.unit_price   <> a.unit_price
     or new.discount_pct <> a.approved_discount_pct
     or new.terms_hash   <> a.terms_hash then
    raise exception 'quote terms do not match approved terms of approval %', a.id
      using errcode = 'P0001';
  end if;
  return new;
end $$;
create trigger quotes_enforce_approval before insert or update on quotes
  for each row execute function quotes_enforce_approval();

create table orders (
  id               uuid primary key default gen_random_uuid(),
  order_number     text not null unique
                   default 'SO-' || lpad(nextval('order_number_seq')::text, 6, '0'),
  quote_id         uuid not null unique references quotes(id),       -- one order per quote
  inquiry_id       uuid not null references inquiries(id),
  product_id       uuid not null references products(id),
  quantity         integer not null,
  unit_price       numeric(12,2) not null,
  discount_pct     numeric(5,2) not null,
  subtotal         numeric(14,2) not null,
  discount_amount  numeric(14,2) not null,
  total            numeric(14,2) not null,
  currency         char(3) not null,
  terms_hash       text not null,
  status           text not null default 'confirmed'
                   check (status in ('confirmed','fulfilled','cancelled')),
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);

create or replace function orders_enforce_quote() returns trigger
language plpgsql as $$
declare q quotes%rowtype;
begin
  select * into q from quotes where id = new.quote_id;
  if new.inquiry_id <> q.inquiry_id or new.product_id <> q.product_id
     or new.quantity <> q.quantity or new.unit_price <> q.unit_price
     or new.discount_pct <> q.discount_pct or new.subtotal <> q.subtotal
     or new.discount_amount <> q.discount_amount or new.total <> q.total
     or new.currency <> q.currency or new.terms_hash <> q.terms_hash then
    raise exception 'order terms do not match quote %', q.quote_number using errcode = 'P0001';
  end if;
  return new;
end $$;
create trigger orders_enforce_quote before insert or update on orders
  for each row execute function orders_enforce_quote();

-- Invoices: 'outbound' = generated by us from an order; 'inbound' = received / uploaded
-- (supplier invoices, or invoices submitted for validation). Inbound fields may be missing:
-- that is detected as an exception rather than rejected by the database.
create table invoices (
  id                 uuid primary key default gen_random_uuid(),
  direction          text not null check (direction in ('outbound','inbound')),
  invoice_number     text,
  order_id           uuid references orders(id),
  counterparty_name  text,
  invoice_date       date,
  due_date           date,
  currency           char(3),
  subtotal           numeric(14,2),
  discount_pct       numeric(5,2),
  discount_amount    numeric(14,2),
  tax_amount         numeric(14,2) default 0,
  total              numeric(14,2),
  line_items         jsonb not null default '[]',
  source             text not null check (source in ('generated','uploaded','ai_extracted','manual')),
  raw_text           text,
  fingerprint        text,     -- normalised hash of key fields, for duplicate detection
  status             text not null default 'received'
                     check (status in ('draft','received','validated','blocked','approved','posted','void')),
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now(),
  check (direction = 'inbound' or (order_id is not null and invoice_number is not null))
);
create unique index invoices_outbound_per_order_uq on invoices (order_id)
  where direction = 'outbound' and status <> 'void';
create unique index invoices_outbound_number_uq on invoices (invoice_number)
  where direction = 'outbound';
create index invoices_dup_lookup_idx on invoices (lower(counterparty_name), invoice_number);
create index invoices_fingerprint_idx on invoices (fingerprint);

create table expenses (
  id             uuid primary key default gen_random_uuid(),
  expense_ref    text,
  employee_name  text,
  category       text,
  amount         numeric(14,2),
  currency       char(3),
  expense_date   date,
  description    text,
  receipt_ref    text,
  status         text not null default 'submitted'
                 check (status in ('submitted','validated','blocked','approved','rejected','paid')),
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);

-- ---------------------------------------------------------------------
-- Exceptions (human-in-the-loop queue)
-- ---------------------------------------------------------------------
create table exceptions (
  id              uuid primary key default gen_random_uuid(),
  entity_type     text not null check (entity_type in ('inquiry','approval','invoice','expense','order')),
  entity_id       uuid not null,
  exception_type  text not null
                  check (exception_type in ('duplicate_invoice','amount_mismatch','unauthorized_discount',
                                            'over_threshold','missing_field','unknown_product',
                                            'low_confidence','invalid_ai_output','llm_unavailable',
                                            'approval_timeout','downstream_failure')),
  severity        text not null default 'high' check (severity in ('low','medium','high','critical')),
  details         jsonb not null default '{}',
  ai_explanation  text,          -- AI may explain; it never resolves
  status          text not null default 'open'
                  check (status in ('open','in_review','resolved','dismissed')),
  assigned_role   text,
  resolution      jsonb,
  resolved_by     text,
  resolved_at     timestamptz,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  check ((status in ('resolved','dismissed')) = (resolved_at is not null and resolved_by is not null))
);
-- Re-running validation must not create duplicate open exceptions.
create unique index exceptions_one_open_uq on exceptions (entity_type, entity_id, exception_type)
  where status in ('open','in_review');
create index exceptions_status_idx on exceptions (status);

alter table ai_runs add constraint ai_runs_invoice_fk   foreign key (invoice_id)   references invoices(id);
alter table ai_runs add constraint ai_runs_exception_fk foreign key (exception_id) references exceptions(id);

-- An invoice/expense with an unresolved exception can never be approved, posted or paid.
create or replace function block_if_open_exceptions() returns trigger
language plpgsql as $$
begin
  if new.status in ('approved','posted','paid') and exists (
       select 1 from exceptions
       where entity_type = tg_argv[0] and entity_id = new.id and status in ('open','in_review')) then
    raise exception '% % has unresolved exceptions and cannot move to %', tg_argv[0], new.id, new.status
      using errcode = 'P0001';
  end if;
  return new;
end $$;
create trigger invoices_block_if_open_exceptions before update on invoices
  for each row execute function block_if_open_exceptions('invoice');
create trigger expenses_block_if_open_exceptions before update on expenses
  for each row execute function block_if_open_exceptions('expense');

-- ---------------------------------------------------------------------
-- Idempotency for any mutating API call (duplicate webhooks, n8n retries)
-- ---------------------------------------------------------------------
create table idempotency_keys (
  key            text primary key,
  scope          text not null,
  request_hash   text not null,      -- same key + different body => rejected
  status         text not null default 'in_progress'
                 check (status in ('in_progress','completed','failed')),
  response_code  integer,
  response_body  jsonb,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  expires_at     timestamptz not null default now() + interval '7 days'
);

-- ---------------------------------------------------------------------
-- Audit log (append-only evidence trail)
-- ---------------------------------------------------------------------
create table audit_log (
  id           bigint generated always as identity primary key,
  occurred_at  timestamptz not null default now(),
  request_id   text,
  inquiry_id   uuid,            -- correlation: lets us rebuild one request's full story
  entity_type  text not null,
  entity_id    uuid,
  action       text not null,   -- e.g. inquiry.received, ai.extracted, rule.decided, approval.modified
  actor        text not null,   -- 'system' | 'ai:<model>' | 'n8n' | 'user:<name>'
  before       jsonb,
  after        jsonb,
  metadata     jsonb not null default '{}'
);
create index audit_log_inquiry_idx on audit_log (inquiry_id, occurred_at);
create index audit_log_entity_idx  on audit_log (entity_type, entity_id, occurred_at);

create or replace function audit_log_append_only() returns trigger
language plpgsql as $$
begin
  raise exception 'audit_log is append-only' using errcode = 'P0001';
end $$;
create trigger audit_log_no_update_delete before update or delete on audit_log
  for each row execute function audit_log_append_only();
create trigger audit_log_no_truncate before truncate on audit_log
  for each statement execute function audit_log_append_only();

-- ---------------------------------------------------------------------
-- updated_at triggers + lock down Supabase REST access
-- ---------------------------------------------------------------------
do $$
declare t text;
begin
  foreach t in array array['products','customers','inquiries','approvals','appointments','quotes',
                           'orders','invoices','expenses','exceptions','idempotency_keys'] loop
    execute format('create trigger %I_set_updated_at before update on %I
                    for each row execute function set_updated_at()', t, t);
  end loop;

  foreach t in array array['products','customers','inquiries','ai_runs','rule_decisions','approvals',
                           'appointments','quotes','orders','invoices','expenses','exceptions',
                           'idempotency_keys','audit_log'] loop
    execute format('alter table %I enable row level security', t);
  end loop;
end $$;
