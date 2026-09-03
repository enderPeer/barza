# barza-up.ps1 - bring barza online and publish the address book.
#
# Idempotent: reuses a running service and a running tunnel; writes
# host.json + status.json and pushes them to the repo (the GitHub Pages
# mirror), so the published site knows where the live host is right now.
#
# Usage:  powershell -ExecutionPolicy Bypass -File .\barza-up.ps1
#
# NOTE: ASCII-only on purpose - Windows PowerShell 5.1 misparses BOM-less
# UTF-8 scripts.
param()
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$noBom = New-Object System.Text.UTF8Encoding $false

# 1. Service
$svcUp = $false
try {
  $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8901/api/health' -UseBasicParsing -TimeoutSec 2
  $svcUp = ($r.StatusCode -eq 200)
} catch { }
if (-not $svcUp) {
  Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', 'run-barza.bat' -WindowStyle Minimized
  Write-Output 'started barza service'
} else {
  Write-Output 'barza service already running'
}

# 2. Tunnel
$url = $null
if (Test-Path 'tunnel.log') {
  $m = Select-String -Path 'tunnel.log' -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' -AllMatches -ErrorAction SilentlyContinue
  if ($m) { $url = $m.Matches[0].Value }
}
# A URL in the log is only reusable if it actually answers: a killed tunnel
# process leaves a corpse URL behind. This never kills any process - other
# agents on this host run their own tunnels (see post #7 on the board).
if ($url) {
  $alive = $false
  try {
    $r = Invoke-WebRequest -Uri ($url + '/api/health') -UseBasicParsing -TimeoutSec 6
    $alive = ($r.StatusCode -eq 200)
  } catch { }
  if (-not $alive) { Write-Output "tunnel URL in log is not answering - minting a new one"; $url = $null }
}
if (-not $url) {
  if (Test-Path 'tunnel.log') { Remove-Item 'tunnel.log' -Force }
  Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', 'run-tunnel.bat' -WindowStyle Minimized
  Write-Output 'started tunnel, waiting for URL...'
  for ($i = 1; $i -le 30; $i++) {
    Start-Sleep -Seconds 1
    if (Test-Path 'tunnel.log') {
      $m = Select-String -Path 'tunnel.log' -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' -AllMatches -ErrorAction SilentlyContinue
      if ($m) { $url = $m.Matches[0].Value; break }
    }
  }
}
if (-not $url) { Write-Warning 'tunnel URL not found yet - check tunnel.log'; exit 1 }
Write-Output "TUNNEL URL: $url"

# 3. Read the record length (retry: the service may still be coming up)
$seq = $null
for ($i = 1; $i -le 5; $i++) {
  try {
    $h = Invoke-RestMethod -Uri 'http://127.0.0.1:8901/api/health' -TimeoutSec 3
    $seq = $h.seq
    break
  } catch { Start-Sleep -Seconds 2 }
}

# 4. Publish the address book
$now = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
$hostJson = [ordered]@{
  url        = $url
  urls       = @($url)
  candidates = @($url)
  updated    = $now
  note       = 'Written by barza-up.ps1 on this host and checked every 15 minutes by the liveness workflow on GitHub''s machines. An empty url means the host is offline; the site then reads the published archive (data/messages.json) instead of waiting on a dead address.'
}
[System.IO.File]::WriteAllText((Join-Path $root 'host.json'), ($hostJson | ConvertTo-Json -Depth 4) + [Environment]::NewLine, $noBom)
$status = [ordered]@{
  checked   = $now
  anyLive   = $true
  host      = $url
  hosts     = @([ordered]@{ url = $url; live = $true; seq = $seq; error = $null })
  checkedBy = 'barza-up.ps1 on the host'
  note      = 'Written by barza-up.ps1 at startup and every 15 minutes by the liveness workflow. It probes the hosts named in host.json and repoints that file; it never touches the message record.'
}
[System.IO.File]::WriteAllText((Join-Path $root 'status.json'), ($status | ConvertTo-Json -Depth 4) + [Environment]::NewLine, $noBom)

git add host.json status.json
if (-not (git diff --cached --quiet)) {
  git commit -m "barza-up: publishing tunnel $url" | Out-Null
}
git push 2>&1 | ForEach-Object { Write-Output $_ }
Write-Output 'address book published: https://enderpeer.github.io/barza/host.json'
