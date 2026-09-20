-- =====================================================================
-- Control verification — proves the database itself enforces the key rules.
-- Safe to run any time: every test undoes its own changes (nothing is kept).
-- Run in Supabase SQL Editor, or:  psql "$DATABASE_URL" -f db/verify_controls.sql
-- Every row should say PASS.
-- =====================================================================
create temp table if not exists control_checks (n int, control text, result text);
truncate control_checks;

do $$
declare
  r        text[] := '{}';
  v_prod   uuid;
  v_inq    uuid;
  v_appr   uuid;
  v_quote  uuid;
  v_inv    uuid;
  v_key    text := 'verify-' || gen_random_uuid();
begin
  begin
    select id into v_prod from products where sku = 'PRD-X';
    insert into inquiries (idempotency_key, raw_message)
      values (v_key, 'We need 200 units of Product X, 20% discount') returning id into v_inq;
    insert into approvals (inquiry_id, required_role, product_id, quantity, unit_price, requested_discount_pct)
      values (v_inq, 'manager', v_prod, 200, 50.00, 20) returning id into v_appr;

    -- 1. No quote from a pending (not yet approved) request
    begin
      insert into quotes (inquiry_id, approval_id, product_id, quantity, unit_price, discount_pct,
                          subtotal, discount_amount, total, currency, terms_hash)
        values (v_inq, v_appr, v_prod, 200, 50, 20, 10000, 2000, 8000, 'USD', 'x');
      r := array_append(r, 'FAIL: quote was created from a pending approval');
    exception when others then r := array_append(r, 'PASS: ' || sqlerrm); end;

    -- Manager modifies 20% -> 12%
    update approvals set status = 'modified', approved_discount_pct = 12, terms_hash = 'hash-12',
           decided_by = 'user:manager', decided_at = now()
     where id = v_appr;

    -- 2. Quote cannot use the requested 20% instead of the approved 12%
    begin
      insert into quotes (inquiry_id, approval_id, product_id, quantity, unit_price, discount_pct,
                          subtotal, discount_amount, total, currency, terms_hash)
        values (v_inq, v_appr, v_prod, 200, 50, 20, 10000, 2000, 8000, 'USD', 'hash-12');
      r := array_append(r, 'FAIL: quote with unapproved 20% discount was accepted');
    exception when others then r := array_append(r, 'PASS: ' || sqlerrm); end;

    -- 3. Quote arithmetic must be correct
    begin
      insert into quotes (inquiry_id, approval_id, product_id, quantity, unit_price, discount_pct,
                          subtotal, discount_amount, total, currency, terms_hash)
        values (v_inq, v_appr, v_prod, 200, 50, 12, 10000, 1200, 9000, 'USD', 'hash-12');
      r := array_append(r, 'FAIL: quote with wrong total was accepted');
    exception when others then r := array_append(r, 'PASS: ' || sqlerrm); end;

    -- 4. Correct quote (approved 12%) IS accepted
    begin
      insert into quotes (inquiry_id, approval_id, product_id, quantity, unit_price, discount_pct,
                          subtotal, discount_amount, total, currency, terms_hash)
        values (v_inq, v_appr, v_prod, 200, 50, 12, 10000, 1200, 8800, 'USD', 'hash-12')
        returning id into v_quote;
      r := array_append(r, 'PASS: quote with approved 12% accepted (total 8800.00)');
    exception when others then r := array_append(r, 'FAIL: valid quote rejected: ' || sqlerrm); end;

    -- 5. No second quote for the same approval (duplicate protection)
    begin
      insert into quotes (inquiry_id, approval_id, product_id, quantity, unit_price, discount_pct,
                          subtotal, discount_amount, total, currency, terms_hash)
        values (v_inq, v_appr, v_prod, 200, 50, 12, 10000, 1200, 8800, 'USD', 'hash-12');
      r := array_append(r, 'FAIL: duplicate quote was created');
    exception when others then r := array_append(r, 'PASS: ' || sqlerrm); end;

    -- 6. A decided approval cannot be changed afterwards
    begin
      update approvals set approved_discount_pct = 20 where id = v_appr;
      r := array_append(r, 'FAIL: decided approval was modified');
    exception when others then r := array_append(r, 'PASS: ' || sqlerrm); end;

    -- 7. Order must match its quote exactly
    begin
      insert into orders (quote_id, inquiry_id, product_id, quantity, unit_price, discount_pct,
                          subtotal, discount_amount, total, currency, terms_hash)
        values (v_quote, v_inq, v_prod, 200, 50, 20, 10000, 2000, 8000, 'USD', 'hash-12');
      r := array_append(r, 'FAIL: order with different terms than quote was accepted');
    exception when others then r := array_append(r, 'PASS: ' || sqlerrm); end;

    -- 8. Invoice with an open exception cannot be approved
    insert into invoices (direction, invoice_number, counterparty_name, total, source)
      values ('inbound', 'VERIFY-001', 'Verify Supplier', 100, 'manual') returning id into v_inv;
    insert into exceptions (entity_type, entity_id, exception_type)
      values ('invoice', v_inv, 'amount_mismatch');
    begin
      update invoices set status = 'approved' where id = v_inv;
      r := array_append(r, 'FAIL: invoice with open exception was approved');
    exception when others then r := array_append(r, 'PASS: ' || sqlerrm); end;

    -- 9. Duplicate webhook (same idempotency key) is rejected
    begin
      insert into inquiries (idempotency_key, raw_message) values (v_key, 'duplicate delivery');
      r := array_append(r, 'FAIL: duplicate inquiry was accepted');
    exception when others then r := array_append(r, 'PASS: ' || sqlerrm); end;

    -- 10. Audit log is append-only
    insert into audit_log (entity_type, action, actor) values ('test', 'verify.controls', 'system');
    begin
      update audit_log set actor = 'someone-else' where action = 'verify.controls';
      r := array_append(r, 'FAIL: audit_log row was edited');
    exception when others then r := array_append(r, 'PASS: ' || sqlerrm); end;

    raise exception 'undo-test-data';
  exception when others then
    if sqlerrm <> 'undo-test-data' then r := array_append(r, 'FAIL: setup error: ' || sqlerrm); end if;
  end;

  insert into control_checks (n, control, result)
  select i,
         (array['No quote from pending approval','Quote cannot exceed approved discount',
                'Quote arithmetic enforced','Approved quote accepted','One quote per approval',
                'Decided approval is frozen','Order must match quote',
                'Open exception blocks invoice approval','Duplicate webhook rejected',
                'Audit log append-only'])[i],
         r[i]
  from generate_subscripts(r, 1) as i;
end $$;

select n, control, result from control_checks order by n;
