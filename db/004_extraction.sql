-- 004: support AI extraction results on the inquiry.
--  * new status 'extracted' (AI output validated, ready for the rules engine)
--  * extracted: normalised, validated extraction (or partial data when human review is needed)
--  * accepted_ai_run_id: which AI run produced the accepted data (evidence link)
alter table inquiries drop constraint if exists inquiries_status_check;
alter table inquiries add constraint inquiries_status_check
  check (status in ('received','extracting','extracted','needs_review','awaiting_approval',
                    'approved','rejected','quoted','ordered','invoiced','failed','cancelled'));

alter table inquiries add column if not exists extracted jsonb;
alter table inquiries add column if not exists accepted_ai_run_id uuid references ai_runs(id);
