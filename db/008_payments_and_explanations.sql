-- 008: payment status on invoices (assignment section 7) and a guard that only an approved
-- invoice can be marked as paid.
alter table invoices add column if not exists payment_status text not null default 'unpaid';
alter table invoices drop constraint if exists invoices_payment_status_check;
alter table invoices add constraint invoices_payment_status_check
  check (payment_status in ('unpaid','partially_paid','paid'));
alter table invoices add column if not exists amount_paid numeric(14,2) not null default 0;
alter table invoices add column if not exists paid_at timestamptz;
alter table invoices add column if not exists payment_reference text;

-- Money can only move against an invoice that passed validation and was approved.
create or replace function invoices_payment_requires_approval() returns trigger
language plpgsql as $$
begin
  if new.payment_status <> 'unpaid' and new.status not in ('approved','posted') then
    raise exception 'invoice % is % and cannot be marked as paid', new.id, new.status
      using errcode = 'P0001';
  end if;
  if new.amount_paid < 0 or (new.total is not null and new.amount_paid > new.total) then
    raise exception 'amount paid % is not valid for invoice total %', new.amount_paid, new.total
      using errcode = 'P0001';
  end if;
  return new;
end $$;
drop trigger if exists invoices_payment_guard on invoices;
create trigger invoices_payment_guard before update on invoices
  for each row execute function invoices_payment_requires_approval();
