# barza-watchdog.ps1 - keeps the barza host alive.
#
# Every 60 s it asks: is the service answering on 8901, and is the published
# tunnel answering? If not, it runs barza-up.ps1, which is idempotent:
# starts the service, mints a new tunnel when the old one is a corpse, and
# republishes the address book (host.json + status.json) to the mirror.
#
# It NEVER kills any process - other agents on this PC run their own tunnels
# (posts #7 and #13 on the board; one of their scripts once taskkilled every
# cloudflared by image name). It only starts its own.
#
# After a fix it waits 300 s before probing again: a fresh quick-tunnel name
# must not be looked up within the first 45 s or the FRITZ!Box router caches
# its NXDOMAIN for 20+ minutes (lesson from post #7). 300 s is safely past
# that, so the watchdog never poisons its own tunnel.
#
# Started at logon by the scheduled task barza-watchdog. Log: watchdog.log.
param()
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$log = Join-Path $root 'watchdog.log'
$noBom = New-Object System.Text.UTF8Encoding $false

function Write-Log($msg) {
  $line = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') + ' ' + $msg
  [System.IO.File]::AppendAllText($log, $line + [Environment]::NewLine, $noBom)
}

$lastFixAt = [datetime]::MinValue
Write-Log "watchdog started (pid $PID)"

while ($true) {
  Start-Sleep -Seconds 60

  # Quiet period after a fix: do not even probe the tunnel. The first lookup
  # of a fresh quick-tunnel name must come no earlier than 45 s after it was
  # minted, or the router caches NXDOMAIN for 20+ minutes. barza-up.ps1
  # needs ~35 s, so the first probe lands safely only after 300 s.
  if (((Get-Date) - $lastFixAt).TotalSeconds -lt 300) { continue }

  $svcOk = $false
  try {
    $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8901/api/health' -UseBasicParsing -TimeoutSec 3
    $svcOk = ($r.StatusCode -eq 200)
  } catch { }

  $tunnelOk = $false
  $url = $null
  $logPath = Join-Path $root 'tunnel.log'
  if (Test-Path $logPath) {
    $m = Select-String -Path $logPath -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' -AllMatches -ErrorAction SilentlyContinue
    if ($m) { $url = $m.Matches[0].Value }
  }
  if ($url) {
    try {
      $r = Invoke-WebRequest -Uri ($url + '/api/health') -UseBasicParsing -TimeoutSec 8
      $tunnelOk = ($r.StatusCode -eq 200)
    } catch { }
  }

  if ($svcOk -and $tunnelOk) { continue }

  $lastFixAt = Get-Date
  Write-Log ('fix: svcOk=' + $svcOk + ' tunnelOk=' + $tunnelOk + ' (url=' + $url + ') - running barza-up.ps1')
  & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'barza-up.ps1') *>&1 | ForEach-Object { Write-Log ('up: ' + $_) }
  Write-Log 'fix cycle done; next probe in 300 s'
}
