# cloudflared install for `zhub-server --public-tunnel`

`zhub-server` can spawn an ephemeral Cloudflare Tunnel and print a `*.trycloudflare.com` URL when started with `--public-tunnel`. This requires the `cloudflared` binary on your PATH. **No Cloudflare account needed** for ephemeral tunnels.

Verified live on 2026-05-09 — round-tripped a chat completion through `https://*.trycloudflare.com → hub → publisher → response` end-to-end. The install path below is exactly what was used.

## Install

### Static binary (any Linux, no apt needed)

This is what works inside proot, Termux, or any minimal Linux environment:

```bash
ARCH=$(uname -m)
case "$ARCH" in
  x86_64)  BIN=cloudflared-linux-amd64 ;;
  aarch64) BIN=cloudflared-linux-arm64 ;;
  armv7l)  BIN=cloudflared-linux-arm ;;
  *) echo "unsupported $ARCH"; exit 1 ;;
esac
sudo curl -fsSL \
  "https://github.com/cloudflare/cloudflared/releases/latest/download/$BIN" \
  -o /usr/local/bin/cloudflared
sudo chmod +x /usr/local/bin/cloudflared
```

### Debian / Ubuntu (with apt)

```bash
ARCH=$(dpkg --print-architecture)
curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}.deb" -o /tmp/cf.deb
sudo dpkg -i /tmp/cf.deb
```

### macOS

```bash
brew install cloudflared
```

### Verify

```bash
cloudflared --version
# cloudflared version YYYY.MM.D ...
```

## Use

```bash
zhub-server --port 8080 --public-tunnel
```

In about 5 seconds:

```
INFO zhub.tunnel: starting cloudflared: /usr/local/bin/cloudflared tunnel --url http://localhost:8080 --no-autoupdate
INFO zhub.tunnel: tunnel up at https://random-words.trycloudflare.com
INFO zhub.persistence: persistence opened at zhub.db
INFO:     Started server process [...]
INFO:     Uvicorn running on http://0.0.0.0:8080 (Press CTRL+C to quit)
```

Anything you publish locally (`hub_url=ws://127.0.0.1:8080`) is now reachable from the world via that URL.

```bash
curl https://random-words.trycloudflare.com/<your-ai>/v1/chat/completions \
  -H "Authorization: Bearer zk_..." \
  -d '{"messages":[{"role":"user","content":"hi"}]}'
```

## Caveats

- **The URL is ephemeral.** Different on every restart of `cloudflared`. For a stable URL, configure a named tunnel in the Cloudflare dashboard and front the hub behind it the normal way; that's outside zhub's scope but well-documented at https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/.
- **Cloudflare quickstart tunnels are intended for testing.** Don't share the URL with the public internet at scale; use a named tunnel + Access for that.
- **Both processes must stay alive.** Killing `cloudflared` alone leaves the hub running but unreachable; killing the hub alone leaves cloudflared 502'ing requests. `zhub-server --public-tunnel` ties their lifecycles — Ctrl+C on the hub also tears down cloudflared.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `cloudflared not found on PATH` | binary not installed | re-run install above |
| `did not produce a public URL within 30s` | egress blocked or proxy interfering | run `cloudflared tunnel --url http://localhost:8080` manually to see CF's actual error |
| URL changes on every restart | ephemeral tunnels rotate | use a named tunnel for stability |
| `502` from public URL | hub crashed but cloudflared still up | check `zhub-server` log; `pkill cloudflared` then restart |
| Tunnel works briefly then hangs | inactive idle timeout from CF edge | reconnect; for production traffic use a named tunnel |
| `proot` exit 144 immediately on launch | shell sub-process bookkeeping in proot — process actually fine | use `nohup ... &` + `disown`, verify via `curl <url>/healthz` |
