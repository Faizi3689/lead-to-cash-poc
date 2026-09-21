# Sample invoice texts for POST /v1/invoices/from-text

## Clean supplier invoice (validates)
```
Invoice No: SUP-7788
From: Northwind Supplies
Date: 2026-09-20
Currency: USD
Subtotal: 1,000.00
Discount (5%): -50.00
Tax: 0.00
Total Due: 950.00
```

## Invoice against an order with more discount than approved (blocked: unauthorized_discount)
Replace SO-000001 with a real order number from your database.
```
Invoice No: SUP-9001
From: Northwind Supplies
Order Ref: SO-000001
Date: 2026-09-20
Currency: USD
Subtotal: 10,000.00
Discount (20%): -2,000.00
Tax: 0.00
Total Due: 8,000.00
```

## Totals that do not add up (blocked: amount_mismatch)
```
Invoice No: SUP-9002
From: Contoso Ltd
Date: 2026-09-21
Currency: USD
Subtotal: 2,000.00
Tax: 0.00
Total Due: 2,500.00
```

Add `[[mock:timeout]]` to any text (mock mode) to simulate the AI being down during capture.
