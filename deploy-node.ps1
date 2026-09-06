# deploy-node.ps1 - make a Linux box on the LAN a barza node, from this
# workstation. Idempotent; re-run to update a node to the current main.
#
#   powershell -ExecutionPolicy Bypass -File .\deploy-node.ps1 -Node knecht24
#
# Needs: `ssh <Node>` working without a prompt (an entry in ~/.ssh/config
# with a key), and `gh` logged in to the account that owns the repo, so the
# node's deploy key can be registered. On the node it needs python3 >= 3.10,
# git, curl and passwordless sudo (apt + systemd).
#
# What it does:
#   1. on the node: a dedicated ed25519 deploy key (~/.ssh/id_ed25519_barza_deploy),
#      GitHub's published ssh host keys in known_hosts, and an ssh config
#      block that uses that key for github.com
#   2. here: registers the public key as a WRITE deploy key of the repo
#      (title "<node> barza service") unless one with that title exists
#   3. on the node: clones the repo over ssh into ~/<RemoteRoot>, or updates
#      an existing clone (service stopped first, pull --rebase, no force)
#   4. on the node: linux/install.sh (cloudflared, git identity, systemd
#      units, barza.service started; with -Tunnel also the tunnel + watchdog
#      and a first publish of the address book)
#   5. from here: health of the node's LAN address
#
# NOTE: ASCII-only on purpose - Windows PowerShell 5.1 misparses BOM-less
# UTF-8 scripts.
param(
  [string]$Node = 'knecht24',
  [string]$Repo = 'enderPeer/barza',
  [string]$RemoteRoot = 'barza',
  [string]$LanIp = '',
  [switch]$Tunnel
)
$ErrorActionPreference = 'Stop'
$keyTitle = "$Node barza service"

function Invoke-Ssh([string]$script) {
  # cmd merges the streams, so a stderr line never becomes a terminating
  # error in Windows PowerShell 5.1.
  $tmp = [System.IO.Path]::GetTempFileName()
  [System.IO.File]::WriteAllText($tmp, $script.Replace("`r`n", "`n"), (New-Object System.Text.UTF8Encoding $false))
  try {
    $out = & cmd.exe /c ('ssh -o BatchMode=yes -o ConnectTimeout=10 ' + $Node + ' bash -s < "' + $tmp + '" 2>&1')
    $code = $LASTEXITCODE
  } finally { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
  $text = ($out | Out-String).TrimEnd()
  if ($code -ne 0) { throw "ssh $Node failed ($code):`n$text" }
  return $text
}

Write-Output "== 1. deploy key + github host keys on $Node =="
$pub = Invoke-Ssh @'
set -e
mkdir -p ~/.ssh && chmod 700 ~/.ssh
if [ ! -f ~/.ssh/id_ed25519_barza_deploy ]; then
  ssh-keygen -q -t ed25519 -N "" -C "barza-deploy@$(hostname)" -f ~/.ssh/id_ed25519_barza_deploy
fi
chmod 600 ~/.ssh/id_ed25519_barza_deploy
# GitHub's host keys from its own API, not from the first connection
curl -fsS -m 15 https://api.github.com/meta \
  | python3 -c 'import json,sys; [print("github.com "+k) for k in json.load(sys.stdin)["ssh_keys"]]' > /tmp/gh_known
grep -v "^github.com " ~/.ssh/known_hosts 2>/dev/null > /tmp/kh_rest || true
cat /tmp/kh_rest /tmp/gh_known > ~/.ssh/known_hosts && chmod 600 ~/.ssh/known_hosts
rm -f /tmp/gh_known /tmp/kh_rest
if ! grep -q "barza deploy key" ~/.ssh/config 2>/dev/null; then
  printf "# barza deploy key (write access to the barza repo only)\nHost github.com\n  User git\n  IdentityFile ~/.ssh/id_ed25519_barza_deploy\n  IdentitiesOnly yes\n" >> ~/.ssh/config
fi
chmod 600 ~/.ssh/config
cat ~/.ssh/id_ed25519_barza_deploy.pub
'@
$pubLine = ($pub -split "`n" | Where-Object { $_ -match '^ssh-ed25519 ' } | Select-Object -Last 1)
if (-not $pubLine) { throw "no public key came back from $Node" }
Write-Output "public key: $pubLine"

Write-Output "== 2. register the deploy key on $Repo =="
$existing = & gh repo deploy-key list -R $Repo 2>&1 | Out-String
if ($existing -match [regex]::Escape($keyTitle)) {
  Write-Output "deploy key '$keyTitle' already registered"
} else {
  $tmpKey = Join-Path $env:TEMP ("barza-deploy-" + $Node + ".pub")
  [System.IO.File]::WriteAllText($tmpKey, $pubLine + "`n", (New-Object System.Text.UTF8Encoding $false))
  try {
    & gh repo deploy-key add $tmpKey -R $Repo --allow-write -t $keyTitle
    if ($LASTEXITCODE -ne 0) { throw "gh repo deploy-key add failed" }
  } finally { Remove-Item $tmpKey -Force -ErrorAction SilentlyContinue }
  Write-Output "registered write deploy key '$keyTitle'"
}

Write-Output "== 3. checkout on $Node (~/$RemoteRoot) =="
$sshUrl = "git@github.com:$Repo.git"
$checkout = @"
set -e
# -n: this script arrives on bash's stdin, and an ssh without -n would read
# the rest of it as its own input.
ssh -n -o BatchMode=yes -T git@github.com 2>&1 | grep -q "successfully authenticated" || { echo "github ssh auth failed (is the deploy key registered?)"; exit 1; }
if [ -d ~/$RemoteRoot/.git ]; then
  cd ~/$RemoteRoot
  if systemctl is-active --quiet barza.service 2>/dev/null; then sudo systemctl stop barza.service; echo "stopped barza.service for the update"; fi
  git remote set-url origin $sshUrl
  git fetch -q origin main </dev/null
  git rebase -q origin/main </dev/null || { git rebase --abort; sudo systemctl start barza.service; echo "rebase failed - resolve by hand (service restarted on the old code)"; exit 1; }
  echo "updated: `$(git log -1 --format='%h %s')"
else
  git clone -q $sshUrl ~/$RemoteRoot </dev/null
  echo "cloned: `$(git -C ~/$RemoteRoot log -1 --format='%h %s')"
fi
"@
Write-Output (Invoke-Ssh $checkout)

Write-Output "== 4. linux/install.sh on $Node =="
$envPrefix = ''
if ($LanIp) { $envPrefix = "BARZA_LAN_IP=$LanIp " }
if ($Tunnel) { $envPrefix += 'BARZA_TUNNEL=1 ' }
$install = Invoke-Ssh ("set -e; cd ~/$RemoteRoot && " + $envPrefix + 'bash linux/install.sh')
Write-Output $install
$m = [regex]::Match($install, 'BARZA_LAN_IP=([0-9.]+)')
if ($m.Success) { $LanIp = $m.Groups[1].Value }

Write-Output "== 5. health from here =="
if (-not $LanIp) { throw 'the node did not report its LAN address' }
$h = Invoke-RestMethod -Uri ("http://" + $LanIp + ":8901/api/health") -TimeoutSec 5
Write-Output ("node " + $Node + " at http://" + $LanIp + ":8901 -> host=" + $h.host + " version=" + $h.version + " seq=" + $h.seq + " messages=" + $h.messages)
Write-Output "done. This workstation's relay: barza-up.ps1 (BARZA_UPSTREAM defaults to knecht24)."
