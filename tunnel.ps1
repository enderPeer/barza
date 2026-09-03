$site = "https://enderPeer.github.io/barza/"
Write-Host "Tunneling $site over Cloudflare quick tunnel..." -ForegroundColor Cyan
cloudflared tunnel --url $site --no-autoupdate
