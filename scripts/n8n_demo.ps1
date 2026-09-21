<#
  Send demo requests to the n8n webhooks.
    .\scripts\n8n_demo.ps1 -N8nBase "https://<you>.app.n8n.cloud"          # active workflows
    .\scripts\n8n_demo.ps1 -N8nBase "https://<you>.app.n8n.cloud" -Test    # test URLs
    .\scripts\n8n_demo.ps1 -N8nBase "..." -Scenario invoice-tampered -OrderNumber SO-000003
  Scenarios: manager (default), auto, reject, review, invoice-clean, invoice-tampered, invoice-duplicate
#>
param(
  [Parameter(Mandatory = $true)][string]$N8nBase,
  [switch]$Test,
  [string]$Scenario = "manager",
  [string]$OrderNumber = "SO-000001"
)
$prefix = if ($Test) { "webhook-test" } else { "webhook" }

function Send($path, $body) {
  $uri = "$N8nBase/$prefix/$path"
  try { $r = Invoke-RestMethod -Method Post -Uri $uri -ContentType "application/json" -Body ($body | ConvertTo-Json -Depth 6) }
  catch {
    $resp = $_.Exception.Response
    $text = if ($resp) { (New-Object System.IO.StreamReader($resp.GetResponseStream())).ReadToEnd() } else { $_.Exception.Message }
    Write-Host "HTTP error: $text" -ForegroundColor Red; return
  }
  $r | ConvertTo-Json -Depth 8
  if ($r.approval_form_url) {
    Write-Host "`nApprover link (open in a browser, choose 'modify' and enter 12):" -ForegroundColor Cyan
    Write-Host $r.approval_form_url -ForegroundColor Green
  }
}

$stamp = Get-Date -Format "HHmmss"
switch ($Scenario) {
  "manager" { Send "inquiry" @{ message = "We need 200 units of Product X next month, can you give us 20% discount? I'd like to talk tomorrow."; customer_name = "Sarah Lee"; customer_email = "sarah.lee@acme-trading.example"; message_id = "demo-$stamp" } }
  "auto"    { Send "inquiry" @{ message = "Please send 100 units of Product X with 3% discount."; customer_email = "omar.khan@globex.example"; message_id = "demo-$stamp" } }
  "reject"  { Send "inquiry" @{ message = "We need 100 units of Product X with 90% discount."; message_id = "demo-$stamp" } }
  "review"  { Send "inquiry" @{ message = "We want 100 units of Product Q at 10% off."; message_id = "demo-$stamp" } }
  "invoice-clean" { Send "invoice" @{ submission_id = "inv-$stamp"; raw_text = "Invoice No: SUP-$stamp`nFrom: Northwind Supplies`nDate: 2026-09-20`nCurrency: USD`nSubtotal: 1,000.00`nDiscount (5%): -50.00`nTax: 0.00`nTotal Due: 950.00" } }
  "invoice-tampered" { Send "invoice" @{ submission_id = "inv-$stamp"; raw_text = "Invoice No: SUP-T$stamp`nFrom: Northwind Supplies`nOrder Ref: $OrderNumber`nDate: 2026-09-20`nCurrency: USD`nSubtotal: 10,000.00`nDiscount (20%): -2,000.00`nTax: 0.00`nTotal Due: 8,000.00" } }
  "invoice-duplicate" {
    $inv = @{ invoice_number = "SUP-D$stamp"; counterparty_name = "Northwind Supplies"; invoice_date = "2026-09-20"; currency = "USD"; subtotal = "1000.00"; discount_pct = "5"; discount_amount = "50.00"; tax_amount = "0.00"; total = "950.00" }
    Write-Host "First submission:" -ForegroundColor Cyan;  Send "invoice" ($inv + @{ submission_id = "a-$stamp" })
    if ($Test) { Read-Host "Click 'Execute workflow' in n8n again, then press Enter" }
    Write-Host "Second submission of the same invoice:" -ForegroundColor Cyan; Send "invoice" ($inv + @{ submission_id = "b-$stamp" })
  }
  default { Write-Host "Unknown scenario $Scenario" -ForegroundColor Red }
}
