<#
  End-to-end demo of the lead-to-cash flow against a running API.

  Usage (PowerShell, from the project root):
      .\scripts\demo_flow.ps1                                  # local, key read from .env
      .\scripts\demo_flow.ps1 -Base "https://<your-app>.onrender.com" -ApiKey "<key>"
#>
param(
  [string]$Base = "http://127.0.0.1:8000",
  [string]$ApiKey = "",
  [string]$Message = "We need 200 units of Product X next month, can you give us 20% discount? I'd like to talk tomorrow.",
  [decimal]$ModifyTo = 12
)

if (-not $ApiKey) {
  $ApiKey = (Get-Content .env | Select-String '^API_KEY=').Line.Substring(8).Split('#')[0].Trim()
}
$h = @{ "X-API-Key" = $ApiKey }
function Show($title, $obj) {
  Write-Host "`n=== $title ===" -ForegroundColor Cyan
  $obj | ConvertTo-Json -Depth 6
}

# 1. Intake (idempotent: the same key + body always returns the same inquiry)
$key = "demo-$([guid]::NewGuid())"
$body = @{ message = $Message; customer_email = "sarah.lee@acme-trading.example" } | ConvertTo-Json
$inquiry = Invoke-RestMethod -Method Post -Uri "$Base/v1/inquiries" -ContentType "application/json" `
  -Headers ($h + @{ "Idempotency-Key" = $key }) -Body $body
Show "1. Inquiry received" $inquiry

# 2. AI extraction (validated, grounded, product resolved deterministically)
$extract = Invoke-RestMethod -Method Post -Uri "$Base/v1/inquiries/$($inquiry.inquiry_id)/extract" -Headers $h
Show "2. AI extraction" $extract

# 3. Deterministic rules -> approval record
$decision = Invoke-RestMethod -Method Post -Uri "$Base/v1/inquiries/$($inquiry.inquiry_id)/decide" -Headers $h
Show "3. Rules decision" $decision

# 4. Human decision, when one is required
if ($decision.approval_token) {
  $payload = @{ action = "modify"; token = $decision.approval_token; decided_by = "maria.manager"
                approved_discount_pct = $ModifyTo; comment = "Volume does not justify the request" } | ConvertTo-Json
  $decided = Invoke-RestMethod -Method Post -Headers $h -ContentType "application/json" `
    -Uri "$Base/v1/approvals/$($decision.approval.approval_id)/decision" -Body $payload
  Show "4. Manager modified the discount" $decided
  Write-Host "`nApproved discount: $($decided.approval.approved_discount_pct)%  ->  total $($decided.amounts.total)" -ForegroundColor Green
} elseif ($decision.outcome -eq "auto_approve") {
  Write-Host "`nNo human approval needed (outcome: $($decision.outcome))" -ForegroundColor Green
} else {
  Write-Host "`nStopped: outcome is '$($decision.outcome)' - nothing to quote." -ForegroundColor Yellow
  return
}

# 5. Documents - each one generated from the APPROVAL, never from the customer's request
$iid = $inquiry.inquiry_id
$quote = Invoke-RestMethod -Method Post -Uri "$Base/v1/inquiries/$iid/quote" -Headers $h
$order = Invoke-RestMethod -Method Post -Uri "$Base/v1/quotes/$($quote.quote_id)/order" -Headers $h
$invoice = Invoke-RestMethod -Method Post -Uri "$Base/v1/orders/$($order.order_id)/invoice" -Headers $h
Write-Host "`n=== 5. Documents ===" -ForegroundColor Cyan
"{0,-8} {1,-12} discount {2,6}%   total {3}" -f "Quote",   $quote.quote_number,     $quote.discount_pct,   $quote.total
"{0,-8} {1,-12} discount {2,6}%   total {3}" -f "Order",   $order.order_number,     $order.discount_pct,   $order.total
"{0,-8} {1,-12} discount {2,6}%   total {3}  ({4})" -f "Invoice", $invoice.invoice_number, $invoice.discount_pct, $invoice.total, $invoice.status

# 6. Appointment (only if the customer asked for a call)
if ($extract.extracted.appointment_requested) {
  try {
    $appt = Invoke-RestMethod -Method Post -Uri "$Base/v1/inquiries/$iid/appointment" -Headers $h
    Write-Host "`n=== 6. Appointment ===" -ForegroundColor Cyan
    Write-Host "Booked $($appt.scheduled_start) ($($appt.timezone))  ref $($appt.external_ref)  - customer asked: '$($appt.requested_text)'"
  } catch { Write-Host "`nAppointment failed (calendar down?) - safe to retry: $($_.Exception.Message)" -ForegroundColor Yellow }
}

# 7. Evidence: approved == quoted == ordered == invoiced
$summary = Invoke-RestMethod -Uri "$Base/v1/inquiries/$iid/documents" -Headers $h
Write-Host "`n=== 7. Consistency check ===" -ForegroundColor Cyan
Write-Host "Customer asked for $($summary.requested_discount_pct)%, approval granted $($summary.documents.approval.discount_pct)%"
$color = if ($summary.consistent) { "Green" } else { "Red" }
Write-Host "Verdict: $($summary.verdict)" -ForegroundColor $color
