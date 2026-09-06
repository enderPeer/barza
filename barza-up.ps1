# barza-up.ps1 - keep this workstation's side of barza up: the relay.
#
# Since 2026-09-06 the barza SERVICE (the record, the inbox ingest, the git
# push), its Cloudflare tunnel and the address book all live on knecht24:
# http://192.168.178.200:8901 on the LAN, systemd units from linux/ in this
# repo, installed by deploy-node.ps1. What this workstation still runs is a
# RELAY on 127.0.0.1:8901 (barza-relay.py): every agent here that talks to
# the old local address, or drops files into inbox/, keeps working - the
# relay forwards to the node.
#
# Idempotent: starts the relay only if 127.0.0.1:8901 is not answering. It
# publishes nothing - the node does that (linux/barza-up.sh).
#
# Usage:  powershell -ExecutionPolicy Bypass -File .\barza-up.ps1
#         $env:BARZA_UPSTREAM overrides the node address.
#
# NOTE: ASCII-only on purpose - Windows PowerShell 5.1 misparses BOM-less
# UTF-8 scripts.
param()
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$upstream = 'http://192.168.178.200:8901'
if ($env:BARZA_UPSTREAM) { $upstream = $env:BARZA_UPSTREAM.TrimEnd('/') }

function Get-Health($base, $timeout, $path = '/api/health') {
  try {
    $r = Invoke-RestMethod -Uri ($base + $path) -TimeoutSec $timeout
    if ($r.ok) { return $r }
  } catch { }
  return $null
}

# The service lives on the node; nothing here can fix it when it is down
# (ssh knecht24 'systemctl status barza'; journalctl -u barza there).
$h = Get-Health $upstream 4
if (-not $h) {
  Write-Warning "barza service at $upstream is not answering - nothing to do from here"
  exit 1
}
Write-Output ('service ' + $upstream + ' ok: host=' + $h.host + ' version=' + $h.version + ' seq=' + $h.seq)

# /api/relay is the relay's own status (the old local service never had
# it), so this cannot mistake a slow node - or a leftover barza_server.py -
# for a running relay.
if (Get-Health 'http://127.0.0.1:8901' 2 '/api/relay') {
  Write-Output 'barza relay already running on 127.0.0.1:8901'
} else {
  $env:BARZA_UPSTREAM = $upstream
  Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', 'run-relay.bat' -WindowStyle Hidden
  $ok = $false
  for ($i = 1; $i -le 10; $i++) {
    Start-Sleep -Seconds 1
    if (Get-Health 'http://127.0.0.1:8901' 2 '/api/relay') { $ok = $true; break }
  }
  if (-not $ok) { Write-Warning 'relay did not come up - see barza_relay.out'; exit 1 }
  Write-Output ('started barza relay on 127.0.0.1:8901 -> ' + $upstream)
}
Write-Output 'address book: https://enderpeer.github.io/barza/host.json (published by the node)'
