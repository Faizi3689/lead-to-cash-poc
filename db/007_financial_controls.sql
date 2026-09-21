-- 007: financial exception handling
--  * idempotency keys on submitted invoices/expenses: a RETRIED submission returns the same record,
--    while a genuinely new submission of the same invoice is stored and flagged as a duplicate
--  * order_reference keeps what the document said, even if it matches no order
--  * expense fingerprint for duplicate detection
alter table invoices add column if not exists idempotency_key text;
alter table invoices add column if not exists order_reference text;
create unique index if not exists invoices_idempotency_key_uq on invoices (idempotency_key)
  where idempotency_key is not null;

alter table expenses add column if not exists idempotency_key text;
alter table expenses add column if not exists fingerprint text;
create unique index if not exists expenses_idempotency_key_uq on expenses (idempotency_key)
  where idempotency_key is not null;
create index if not exists expenses_fingerprint_idx on expenses (fingerprint);
