# barza-watchdog.ps1 - keeps this workstation's barza relay alive.
#
# Every 60 s it asks two questions: is the barza service on the node
# answering (http://192.168.178.200:8901, knecht24 - see barza-up.ps1), and
# is the relay on 127.0.0.1:8901 answering?
#
#   node down  -> nothing to do from here: systemd restarts the service
#                 there, the node's own watchdog re-mints its tunnel, and
#                 the liveness workflow clears the address book after
#                 15 minutes. Logged once, then it waits.
#   relay down -> barza-up.ps1, which starts it.
#
# It NEVER kills any process. The tunnel is no longer this machine's
# business (it runs on the node), so it is never looked up from here -
# which also retires the FRITZ!Box NXDOMAIN dance from the old version
# (post #7 on the board). After a fix it still waits 300 s before probing
# again, so a slow start is not mistaken for a failure.
#
# Started at logon by the scheduled task barza-watchdog. Log: watchdog.log.
#
# NOTE: ASCII-only on purpose - Windows PowerShell 5.1 misparses BOM-less
# UTF-8 scripts.
param()
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$log = Join-Path $root 'watchdog.log'
$noBom = New-Object System.Text.UTF8Encoding $false
$upstream = 'http://192.168.178.200:8901'
if ($env:BARZA_UPSTREAM) { $upstream = $env:BARZA_UPSTREAM.TrimEnd('/') }

function Write-Log($msg) {
  $line = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') + ' ' + $msg
  [System.IO.File]::AppendAllText($log, $line + [Environment]::NewLine, $noBom)
}
function Test-Health($base, $timeout, $path = '/api/health') {
  try {
    $r = Invoke-WebRequest -Uri ($base + $path) -UseBasicParsing -TimeoutSec $timeout
    return ($r.StatusCode -eq 200)
  } catch { return $false }
}

$lastFixAt = [datetime]::MinValue
$nodeDownLogged = $false
Write-Log "watchdog started (pid $PID) - service expected at $upstream, relay on 127.0.0.1:8901"

while ($true) {
  Start-Sleep -Seconds 60
  if (((Get-Date) - $lastFixAt).TotalSeconds -lt 300) { continue }

  if (-not (Test-Health $upstream 4)) {
    if (-not $nodeDownLogged) {
      Write-Log "node $upstream is not answering - nothing to fix from here; waiting"
      $nodeDownLogged = $true
    }
    continue
  }
  if ($nodeDownLogged) { Write-Log "node $upstream is back"; $nodeDownLogged = $false }

  # the relay's own status, so a slow node never looks like a dead relay
  if (Test-Health 'http://127.0.0.1:8901' 3 '/api/relay') { continue }

  $lastFixAt = Get-Date
  Write-Log 'fix: relay on 127.0.0.1:8901 not answering - running barza-up.ps1'
  & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'barza-up.ps1') *>&1 | ForEach-Object { Write-Log ('up: ' + $_) }
  Write-Log 'fix cycle done; next probe in 300 s'
}
