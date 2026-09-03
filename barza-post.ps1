param(
  [Parameter(Mandatory)][string]$Author,
  [Parameter(Mandatory)][string]$Title,
  [string]$Body = "",
  [ValidateSet("update","announcement","question","alert","result")][string]$Type = "update"
)
$msg = @{ author = $Author; title = $Title; body = $Body; type = $Type }
$json = $msg | ConvertTo-Json -Depth 4
$inbox = Join-Path $PSScriptRoot "inbox"
New-Item -ItemType Directory -Path $inbox -Force | Out-Null
$name = "{0}-{1}.json" -f (Get-Date -Format "yyyyMMddHHmmss"), ([guid]::NewGuid().ToString("N").Substring(0, 8))
Set-Content -Path (Join-Path $inbox $name) -Value $json -Encoding UTF8
Write-Host "posted to barza inbox: $name"
