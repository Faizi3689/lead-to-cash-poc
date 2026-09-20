-- Seed data for the PoC (safe to re-run).
insert into products (sku, name, aliases, unit_price, currency) values
  ('PRD-X', 'Product X', array['product x','prod x','px'],   50.00, 'USD'),
  ('PRD-Y', 'Product Y', array['product y','prod y','py'],  120.00, 'USD'),
  ('PRD-Z', 'Product Z', array['product z','prod z','pz'],   15.50, 'USD')
on conflict (sku) do nothing;

insert into customers (name, email, company) values
  ('Sarah Lee',  'sarah.lee@acme-trading.example', 'Acme Trading'),
  ('Omar Khan',  'omar.khan@globex.example',       'Globex Ltd')
on conflict do nothing;
