-- 003: store a hash of the original request body so a reused Idempotency-Key
-- with a DIFFERENT body can be detected and rejected (HTTP 409).
alter table inquiries add column if not exists request_hash text;
