**English** ｜ [简体中文](README.zh-CN.md)

# ZhiNao (智脑) — Self-Hosted AI Hub

**ZhiNao** is a 7×24 self-hosted AI hub built around a router (ImmortalWrt/OpenWrt) plus a
Windows host. It gives a local agent stack the two things it usually lacks:

1. **a reliable model channel** — multi-provider routing with fallback chains, cooldowns,
   automatic demotion, and per-model request counters; and
2. **a self-maintaining memory** — background daemons that distill conversations, triage
   facts, and keep a structured user profile, watched over by a watchdog that restarts
   anything that dies.

Everything is **zero-dependency** where it matters (Node built-ins on the router, Python
stdlib on the host) and **$0 to run** if you stay on free tiers, which the default routing
is designed to exploit without ever leaving the request unstuck.

---

## Architecture

```
┌─ Clients ────────────────────────────────────────────────────────────┐
│  any OpenAI-compatible app  (agent CLI, IDE plugin, QwenPaw, ...)     │
└──────────────────────────────┬───────────────────────────────────────┘
                               │  http://<router-ip>:4100/v1/chat/completions
┌──────────────────────────────▼───────────────────────────────────────┐
│  MODEL GATEWAY  —  gateway.mjs  ·  Node, zero dependencies            │
│   • 8 named routes          brain / coding / simple / orchestrator /  │
│                             main / chat / cheap / epr                 │
│   • per-route fallback chain, ordered by preference                   │
│   • failure → next channel; repeated failure → 300 s cooldown         │
│   • GET /stats              per-model + per-route request counters    │
│   • route_overrides.json    hot-reloaded, no restart needed           │
└──────┬──────────────────────────────────────────┬────────────────────┘
       │ GET /stats every 5 min                   │ reads 15-min window
┌──────▼──────────────────────────────┐   ┌───────▼──────────────────────┐
│  MEMORY BRAIN — Python daemons      │   │  auto_route.mjs              │
│   • distill   batch topic merge     │   │  ≥3 failures in 15 min →     │
│   • judge     per-turn fact triage  │   │  demote channel to chain end │
│   • profile   structured profile    │   │  (only ever demotes)         │
└──────┬──────────────────────────────┘   └──────────────────────────────┘
       │ probe → relaunch (exit 0 = alive)
┌──────▼───────────────────────────────────────────────────────────────┐
│  WATCHDOG  —  wd.sh on cron (every minute) + services.conf registry   │
│   silent when healthy · INFO on recovery · ERROR + 300 s cooldown     │
└──────────────────────────────────────────────────────────────────────┘
```

### Why this shape

The router is the only always-on machine, so the gateway lives there: low power, no
laptop-sleep problem, and it is already the network's DNS/proxy hop. The memory daemons
run where the conversation history lives (the Windows host), and the router watches them
over SSH because a watchdog that dies with its ward is not a watchdog.

---

## Components

| Path | What it is |
|---|---|
| `gateway/gateway.mjs` | Model gateway. 8 named routes, ordered fallback chains, 300 s cooldown, `/stats`, `/v1/models`, hot-reloaded overrides. |
| `gateway/auto_route.mjs` | Reads the gateway log's last 15 minutes; demotes any channel with ≥3 failures to the end of every chain. Conservative by design: it only demotes, never promotes, and writes an empty override when healthy so previously demoted channels recover. |
| `gateway/epr_reduce.mjs` | EPR (Evidence-Preserving Reduction) — compresses very long transcripts while keeping line-number anchors, so the compression can be audited. |
| `gateway/config.example.json` | Gateway configuration: channels (providers), routes (named chains), tuning. Copy to `config.json` and fill in your keys. |
| `memory-brain/distill/` | Batch distillation: merges conversation batches into topic summaries, preserving sequence ranges. |
| `memory-brain/judge_daemon.py` | Per-turn semantic triage: classifies each new user turn (fact / preference / decision / constraint …) into single-line memory entries. |
| `memory-brain/profile_daemon.py` | Maintains a structured user profile (explicit info + implicit traits) by incrementally applying LLM-produced add/update/delete operations. |
| `memory-brain/gateway_health_pull.py` | Pulls `/stats` every 5 minutes into a dated Markdown health log — the data source for a later "which model is flaky" report. |
| `memory-brain/router/router.py` | Small CLI used by the daemons to call a model through the gateway, with provider fallback. |
| `watchdog/wd.sh` | The watchdog loop. Reads `services.conf`, probes each service over SSH, relaunches on failure, cools down for 300 s after a failed relaunch. |
| `watchdog/services.conf.example` | Service registry: `name\|probe_cmd\|relaunch_cmd\|cooldown_sec`. |
| `windows/probe_*.ps1`, `windows/stop_*.ps1` | Probe (exit 0 = alive) and stop scripts for the daemons on the Windows side. |
| `docs/PLATFORM.md` | The platform's original design notes: state machine, the stdin bug that cost a day, the memory-integration rules. |
| `docs/model-gateway-design.md` | Model-gateway design rationale. |
| `docs/README-EPR.md` | EPR design and self-test notes. |

---

## Quick start

### 1. Gateway (on the router)

```sh
mkdir -p /root/model-gateway
cp gateway/gateway.mjs gateway/auto_route.mjs gateway/epr_reduce.mjs /root/model-gateway/
cp gateway/config.example.json /root/model-gateway/config.json   # then edit
node /root/model-gateway/gateway.mjs
```

`config.json` is the only file you must edit. Each channel is one provider:

```json
{
  "port": 4100,
  "gateway_key": "<GATEWAY_KEY>",
  "cooldown_seconds": 300,
  "channels": [
    {
      "id": "my-provider",
      "base_url": "https://api.example.com/v1",
      "api_key": "<API_KEY>",
      "models": ["some-model-name"]
    }
  ],
  "routes": {
    "brain": [
      { "id": "my-provider", "model": "some-model-name" }
    ]
  }
}
```

A route is just an ordered list. The first entry is tried first; on failure the gateway
moves to the next, and a channel that keeps failing is put in cooldown so later requests
skip it.

Call it like any OpenAI endpoint — the route name is the model name:

```sh
curl http://<router-ip>:4100/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"brain","messages":[{"role":"user","content":"hello"}]}'
```

### 2. Monitoring

```sh
curl -H "Authorization: Bearer $GATEWAY_KEY" http://<router-ip>:4100/stats
```

```json
{
  "uptime_s": 7302,
  "total_requests": 195,
  "by_route":   { "brain": { "requests": 151, "ok": 151, "switched": 2, "failed": 0 } },
  "by_channel": { "my-provider": { "requests": 130, "ok": 128, "switched": 2,
                                   "failed": 0, "cooldowns": 2 } }
}
```

`by_channel` answers "which model got called how many times, and how did it do" —
including how often the gateway had to **switch** away from it and how often it went into
**cooldown**.

### 3. Watchdog (on the router)

```sh
mkdir -p /root/watch/log
cp watchdog/wd.sh watchdog/gateway_probe.sh /root/watch/
cp watchdog/services.conf.example /root/watch/services.conf   # then edit targets
crontab -e   # add: * * * * * /root/watch/wd.sh >/dev/null 2>&1
```

### 4. Memory daemons (on the host)

Install the probe/stop scripts from `windows/`, create scheduled tasks that run each
daemon, then register each service in `services.conf` so the watchdog owns its lifecycle.
See `docs/PLATFORM.md` for the state machine and the three-step "add a service" recipe.

---

## Design notes worth knowing

- **Cooldown, not retry-forever.** A channel that fails is skipped for `cooldown_seconds`.
  One bad provider does not turn every request into a timeout.
- **Automatic demotion is one-way.** `auto_route.mjs` only ever moves channels *down*.
  Recovery is handled by writing an empty override when the log looks healthy, so a
  demoted channel comes back on its own once it stops failing. Nothing silently promotes
  a flaky provider back to the front.
- **Hot reload.** `route_overrides.json` is re-read on an mtime poll, so routing changes
  (and the automatic demotion above) take effect without restarting the gateway.
- **Strict decoding, no silent corruption.** The Python side decodes subprocess output as
  strict UTF-8 and *fails* rather than substituting replacement characters. A successful
  decode that still contains `U+FFFD` is treated as failure — because `U+FFFD`'s UTF-8
  bytes are valid GBK, and re-decoding them produces plausible-looking garbage.
- **The watchdog redirects stdin.** Any command that can read stdin inside a
  `while read … < file` loop must redirect its own stdin, or it will eat the remaining
  lines. Details in `docs/PLATFORM.md`.

---

## Security

This repository is a **sanitized** export. Provider API keys, gateway keys, LAN
addresses, hostnames and usernames have been replaced with placeholders
(`<API_KEY>`, `<GATEWAY_KEY>`, `<ROUTER_IP>`, `<USER>` …). No working credential is
present here.

Treat the gateway key as a real credential when you deploy: it is the only thing
standing between your API keys and anyone who can reach port 4100. Keep the gateway on
your LAN, or put an authenticating reverse proxy in front of it.

---

## License

MIT — see [LICENSE](LICENSE).
