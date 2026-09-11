#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gateway_health_pull.py — 模型网关健康档案沉淀（智脑结合点②，2026-09-10）

每 5 分钟（schtasks GatewayHealthPull）拉取软路由网关 /stats，
追加写入 memory/gateway-health/YYYY-MM-DD.md，供 memory_search 检索。
网关不可达时也留痕（诊断价值）。零依赖（仅标准库）。
"""
import datetime
import json
import os
import sys
import urllib.request

GATEWAY = "http://<ROUTER_IP>:4100/stats"
KEY = "sk-gw-<GATEWAY_KEY>"
TIMEOUT = 20

BASE = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
OUT_DIR = os.path.join(BASE, "memory", "gateway-health")


def _append(line: str) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, datetime.date.today().isoformat() + ".md")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)


def main() -> int:
    try:
        req = urllib.request.Request(GATEWAY, headers={"Authorization": "Bearer " + KEY})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001 网关不可达也要留痕
        _append("- {ts} | PULL_FAIL: {err}\n".format(
            ts=datetime.datetime.now().isoformat(timespec="seconds"), err=e))
        return 1

    ch = data.get("by_channel", {}) or {}
    parts = []
    for k in sorted(ch):
        v = ch[k]
        parts.append(
            "{k}: {ok}ok/{sw}sw/{fail}fail/cd{cd}".format(
                k=k, ok=v.get("ok", 0), sw=v.get("switched", 0),
                fail=v.get("failed", 0), cd=v.get("cooldowns", 0)))
    chs = "; ".join(parts) or "(none)"

    demoted = (data.get("overrides") or {}).get("demoted") or []
    line = "- {start} up={up}s req={req} | {chs} | demoted={demoted}\n".format(
        start=data.get("started_at", "?"), up=data.get("uptime_s", "?"),
        req=data.get("total_requests", "?"), chs=chs, demoted=demoted)
    _append(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
