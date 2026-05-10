# Deploy zhub on a $5 VPS in 10 minutes

Goal: a stable URL that survives reboots and gives you `https://your-domain/...` on a quiet server you forget about.

This guide assumes Ubuntu 22.04+ on a tiny box (1 vCPU, 512 MB RAM is fine for personal-tier loads). Adapt freely for Debian / Arch / Fedora; the steps are the same.

---

## 1. Server prep (90 seconds)

```bash
ssh root@your-vps
apt update && apt install -y python3-venv python3-pip git curl
adduser zhub --disabled-password --gecos ""
su - zhub
```

## 2. Install zhub (60 seconds)

```bash
git clone https://github.com/Zawwarsami16/zhub
cd zhub
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[server,brains]'
python -m zhub doctor   # sanity check
```

## 3. Cloudflare named tunnel — stable URL forever (4 minutes)

A *quick tunnel* (`--public-tunnel`) gives you a random `*.trycloudflare.com` URL that changes every restart. A *named tunnel* keeps the same hostname forever. One-time setup:

```bash
# install cloudflared (linux/amd64)
curl -L --output cloudflared.deb \
    https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared.deb

# log in once — opens a browser link, you authorize a CF zone you control
cloudflared tunnel login

# create a named tunnel (does NOT expose anything yet)
cloudflared tunnel create zhub-prod
# → Created tunnel zhub-prod with id XYZ
# → Tunnel credentials written to ~/.cloudflared/XYZ.json

# point a hostname (must be in a CF zone you own) at the tunnel
cloudflared tunnel route dns zhub-prod hub.example.com
```

Now `hub.example.com` is wired to your tunnel. The tunnel itself isn't running yet — that's step 4.

## 4. Run zhub as a systemd service (3 minutes)

Two services: one for the hub + brain publisher, one for the cloudflared tunnel. Both auto-restart on failure.

`/etc/systemd/system/zhub.service`:

```ini
[Unit]
Description=zhub hub + brain publisher
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=zhub
WorkingDirectory=/home/zhub/zhub
Environment="PATH=/home/zhub/zhub/.venv/bin:/usr/bin"
# Set whichever brain creds you use:
Environment="GROQ_API_KEY=gsk_REDACTED"
ExecStart=/home/zhub/zhub/.venv/bin/python -m zhub up \
    --no-tunnel --port 8080 --name me \
    --db /home/zhub/zhub/zhub.db
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/zhub-tunnel.service`:

```ini
[Unit]
Description=cloudflared named tunnel for zhub
After=zhub.service network-online.target
Requires=zhub.service

[Service]
Type=simple
User=zhub
ExecStart=/usr/bin/cloudflared tunnel --no-autoupdate run zhub-prod
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable + start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now zhub.service zhub-tunnel.service
sudo systemctl status zhub.service zhub-tunnel.service
```

## 5. Verify (60 seconds)

```bash
# health
curl https://hub.example.com/healthz
# → {"status":"ok","publishers":"1"}

# entity (zhub's self-knowledge)
curl https://hub.example.com/entity | head -20

# the AI's manifest
curl https://hub.example.com/me/manifest.json | jq .

# grab the api key the publisher generated (one-time on first start;
# stable forever after because of --db persistence)
sudo journalctl -u zhub.service --no-pager | grep -E "KEY:" | tail -1
```

Paste `https://hub.example.com/me/v1` + the `zk_...` key into Pocket / openai-py / curl / Claude Desktop. Done.

---

## Operational notes

**Logs.** `journalctl -u zhub.service -f` for live tail. Each request shows up at INFO via the `zhub.access` logger:

```
123 GET /healthz 0ms
200 POST /me/v1/chat/completions 412ms ai=me
```

**Metrics.** `curl https://hub.example.com/metrics` returns a JSON snapshot with per-AI request_count, total_latency_ms, max_latency_ms, avg_latency_ms, plus rate-limit / peer-proxy / tool-call counters. Pipe to a collector if you want history.

**Persistence.** `zhub.db` (SQLite) holds publishers + entity extensions. Survives reboots. Back it up the same way you'd back up any small file.

**Updating.** `cd ~/zhub && git pull && pip install -e '.[server,brains]' && sudo systemctl restart zhub.service`. The named tunnel keeps running.

**Resources.** zhub itself is ~30 MB RSS idle. The brain dominates: brain=ollama means a local model burning whatever Ollama burns; brain=groq/openai/cerebras/anthropic means just outbound HTTPS. For a brain-bills-elsewhere setup, a 512 MB / 1 vCPU box runs zhub + cloudflared + a publisher comfortably.

**Multi-AI.** Run more publishers with different `--name`s against the same hub. Each gets its own `zk_` key + URL. The hub doesn't need restart — just spawn another publisher process.

**Rotating the brain.** Stop the publisher service, change `Environment="..."` in the unit file with a new brain's creds, restart. The `zk_` key is preserved (persistence). External clients see no change; brain underneath silently swapped.
