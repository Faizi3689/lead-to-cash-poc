<#
  Financial exception handling demo.
  Usage: .\scripts\demo_exceptions.ps1   (optionally -Base "https://..." -ApiKey "...")
#>
param([string]$Base = "http://127.0.0.1:8000", [string]$ApiKey = "")

if (-not $ApiKey) { $ApiKey = (Get-Content .env | Select-String '^API_KEY=').Line.Substring(8).Split('#')[0].Trim() }
$h = @{ "X-API-Key" = $ApiKey }
function Post($path, $body = $null, $extra = @{}) {
  $params = @{ Method = "Post"; Uri = "$Base$path"; Headers = ($h + $extra); ContentType = "application/json" }
  if ($body) { $params.Body = ($body | ConvertTo-Json -Depth 6) }
  try { Invoke-RestMethod @params }
  catch {
    $r = $_.Exception.Response
    if ($r) { $msg = (New-Object System.IO.StreamReader($r.GetResponseStream())).ReadToEnd() } else { $msg = $_.Exception.Message }
    [pscustomobject]@{ http_error = [int]$r.StatusCode; body = $msg }
  }
}
function Show($title, $v) {
  Write-Host "`n=== $title ===" -ForegroundColor Cyan
  if ($v.http_error) { Write-Host "HTTP $($v.http_error): $($v.body)" -ForegroundColor Yellow; return }
  $color = if ($v.status -eq "blocked") { "Red" } else { "Green" }
  Write-Host "status: $($v.status)" -ForegroundColor $color
  foreach ($e in $v.open_exceptions) { Write-Host (" - [{0}] {1}: {2}" -f $e.severity, $e.exception_type, $e.message) }
}

# --- Scenario 1: invoice edited to the 20% the customer wanted (12% was approved) ---
$inq = Post "/v1/inquiries" @{ message = "We need 200 units of Product X next month, can you give us 20% discount? I'd like to talk tomorrow." } @{ "Idempotency-Key" = "ex-$([guid]::NewGuid())" }
Post "/v1/inquiries/$($inq.inquiry_id)/extract" | Out-Null
$d = Post "/v1/inquiries/$($inq.inquiry_id)/decide"
Post "/v1/approvals/$($d.approval.approval_id)/decision" @{ action = "modify"; token = $d.approval_token; decided_by = "maria.manager"; approved_discount_pct = 12 } | Out-Null
$q = Post "/v1/inquiries/$($inq.inquiry_id)/quote"
$o = Post "/v1/quotes/$($q.quote_id)/order"
$inv = Post "/v1/orders/$($o.order_id)/invoice"
Show "1a. Generated invoice $($inv.invoice_number) validated" (Post "/v1/invoices/$($inv.invoice_id)/validate")

$tampered = Post "/v1/invoices/$($inv.invoice_id)/correct" @{ changes = @{ discount_pct = "20"; discount_amount = "2000.00"; total = "8000.00" }; corrected_by = "eve.editor"; reason = "customer asked for 20%" }
Show "1b. Someone edits it to 20%" $tampered
Show "1c. Try to approve it anyway" (Post "/v1/invoices/$($inv.invoice_id)/approve" @{ by = "fiona.finance" })
$ua = $tampered.open_exceptions | Where-Object { $_.exception_type -eq "unauthorized_discount" }
Show "1d. Try to accept (override) the unauthorised discount" (Post "/v1/exceptions/$($ua.exception_id)/accept" @{ by = "fiona.finance"; reason = "looks fine" })
Show "1e. Finance restores the approved 12% -> reprocessed" (Post "/v1/invoices/$($inv.invoice_id)/correct" @{ changes = @{ discount_pct = "12"; discount_amount = "1200.00"; total = "8800.00" }; corrected_by = "fiona.finance"; reason = "restore approved terms" })
Show "1f. Approve" (Post "/v1/invoices/$($inv.invoice_id)/approve" @{ by = "fiona.finance" })

# --- Scenario 2: duplicate supplier invoice (and a harmless network retry) ---
$num = "SUP-" + (Get-Random -Maximum 99999)
$supplier = @{ invoice_number = $num; counterparty_name = "Northwind Supplies"; invoice_date = "2026-09-20"; currency = "USD"; subtotal = "1000.00"; discount_pct = "5"; discount_amount = "50.00"; tax_amount = "0.00"; total = "950.00" }
Show "2a. Supplier invoice $num" (Post "/v1/invoices" $supplier @{ "Idempotency-Key" = "$num-a" })
$retry = Post "/v1/invoices" $supplier @{ "Idempotency-Key" = "$num-a" }
Write-Host "2b. Network retry (same key) -> replay: $($retry.replay), same record, nothing flagged" -ForegroundColor Green
$dup = Post "/v1/invoices" $supplier @{ "Idempotency-Key" = "$num-b" }
Show "2c. Supplier sends the same invoice again" $dup
Show "2d. Finance voids the duplicate" (Post "/v1/invoices/$($dup.entity_id)/void" @{ by = "fiona.finance"; reason = "confirmed duplicate" })

# --- Scenario 3: expense over threshold without a receipt ---
$exp = Post "/v1/expenses" @{ employee_name = "Ali Raza"; category = "client dinner"; amount = "1500.00"; currency = "USD"; expense_date = "2026-09-18" }
Show "3a. Expense 1500.00 without receipt" $exp
$exp2 = Post "/v1/expenses/$($exp.entity_id)/correct" @{ changes = @{ receipt_ref = "rcpt-2231.pdf" }; corrected_by = "ali.raza"; reason = "receipt uploaded" }
Show "3b. Receipt added" $exp2
$ot = $exp2.open_exceptions | Where-Object { $_.exception_type -eq "over_threshold" }
Show "3c. Manager signs off the amount" (Post "/v1/exceptions/$($ot.exception_id)/accept" @{ by = "mark.manager"; reason = "client dinner pre-approved" })
Show "3d. Approve" (Post "/v1/expenses/$($exp.entity_id)/approve" @{ by = "mark.manager" })
