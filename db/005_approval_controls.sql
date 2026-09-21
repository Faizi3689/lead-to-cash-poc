-- 005: extra guarantees around approvals
--  * an approver can only REDUCE the requested discount, never increase it
--  * fast lookup of approvals that have timed out
alter table approvals drop constraint if exists approvals_approved_not_above_requested;
alter table approvals add constraint approvals_approved_not_above_requested
  check (approved_discount_pct is null or approved_discount_pct <= requested_discount_pct);

create index if not exists approvals_pending_expiry_idx on approvals (expires_at) where status = 'pending';
