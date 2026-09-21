-- 006: commercial documents
--  * invoices carry the approval's terms_hash, so any invoice can be traced back to what was approved
--  * helpful indexes for the mock calendar and document lookups
alter table invoices add column if not exists terms_hash text;
create index if not exists appointments_start_idx on appointments (scheduled_start) where status <> 'cancelled';
create index if not exists quotes_inquiry_idx on quotes (inquiry_id);
create index if not exists orders_inquiry_idx on orders (inquiry_id);
create index if not exists invoices_order_idx on invoices (order_id);
