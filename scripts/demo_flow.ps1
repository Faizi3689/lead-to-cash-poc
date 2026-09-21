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
} else {
  Write-Host "`nNo human approval needed (outcome: $($decision.outcome))" -ForegroundColor Green
}
