[English](README.md) ｜ **简体中文**

# 智脑（ZhiNao）— 自托管 AI 中枢

**智脑** 是一套 7×24 自托管的 AI 中枢：一台软路由（ImmortalWrt/OpenWrt）当骨架，
一台 Windows 主机当记忆体。它补上本地 Agent 栈通常缺的两块：

1. **一条可靠的模型通道** —— 多供应商有序兜底链、失败冷却、自动降级、按模型的调用计数；
2. **一份自己维护的记忆** —— 后台 daemon 负责蒸馏对话、鉴定事实、维护结构化画像，
   由看门狗保证它们死了能自己爬起来。

关键部分**零依赖**（路由器侧只用 Node 内置模块，主机侧只用 Python 标准库）。
全部走免费额度的话**成本 ¥0**，而默认路由就是为「薅免费额度但请求绝不卡死」设计的。

---

## 架构

```
┌─ 客户端 ─────────────────────────────────────────────────────────────┐
│  任何 OpenAI 兼容应用（Agent CLI、IDE 插件、QwenPaw…）                 │
└──────────────────────────────┬───────────────────────────────────────┘
                               │  http://<router-ip>:4100/v1/chat/completions
┌──────────────────────────────▼───────────────────────────────────────┐
│  模型网关  —  gateway.mjs  ·  Node，零依赖                            │
│   • 8 条命名路由   brain / coding / simple / orchestrator /           │
│                   main / chat / cheap / epr                          │
│   • 每条路由一条有序兜底链                                             │
│   • 失败→下一个渠道；反复失败→300 秒冷却                               │
│   • GET /stats     按模型 / 按路由的请求计数                           │
│   • route_overrides.json  热加载，改文件即生效，无需重启               │
└──────┬──────────────────────────────────────────┬────────────────────┘
       │ 每 5 分钟拉 /stats                        │ 读最近 15 分钟窗口
┌──────▼──────────────────────────────┐   ┌───────▼──────────────────────┐
│  记忆中枢 — Python daemon           │   │  auto_route.mjs              │
│   • distill   批量主题合并          │   │  15 分钟内失败 ≥3 次 →        │
│   • judge     逐条事实鉴定          │   │  把渠道挪到所有链尾            │
│   • profile   结构化画像维护        │   │  （只降不升）                 │
└──────┬──────────────────────────────┘   └──────────────────────────────┘
       │ 探活 → 拉起（exit 0 = 活）
┌──────▼───────────────────────────────────────────────────────────────┐
│  看门狗  —  wd.sh（cron 每分钟）+ services.conf 服务注册表             │
│   健康时静默 · 恢复写 INFO · 失败写 ERROR + 300 秒冷却                 │
└──────────────────────────────────────────────────────────────────────┘
```

### 为什么长这样

路由器是唯一 7×24 开着的机器，所以网关放路由器：省电、没有笔记本休眠问题，
而且它本来就是网络里的 DNS/代理跳板。记忆 daemon 放在对话历史所在的 Windows 主机上；
看门狗放路由器、通过 SSH 远程探活——**跟被看守者一起死的看门狗不算看门狗**。

---

## 组件

| 路径 | 说明 |
|---|---|
| `gateway/gateway.mjs` | 模型网关。8 条命名路由、有序兜底链、300 秒冷却、`/stats`、`/v1/models`、覆盖配置热加载。 |
| `gateway/auto_route.mjs` | 扫网关日志最近 15 分钟；失败 ≥3 次的渠道挪到所有链尾。刻意保守：**只降不升**，健康时写空覆盖，让被降级的渠道自动归位。 |
| `gateway/epr_reduce.mjs` | EPR（证据保持压缩）——压缩超长对话正文但保留行号锚点，使压缩结果可审计。 |
| `gateway/config.example.json` | 网关配置：渠道（供应商）、路由（命名链）、调优参数。复制成 `config.json` 填自己的 key。 |
| `memory-brain/distill/` | 批量蒸馏：把对话批次合并成主题摘要，保留 seq 范围。 |
| `memory-brain/judge_daemon.py` | 逐条语义鉴定：给每个新用户轮次分类（事实/偏好/决策/约束…），落单行记忆。 |
| `memory-brain/profile_daemon.py` | 维护结构化画像（显式信息 + 隐式特征）：由 LLM 产出增量 add/update/delete 操作后应用。 |
| `memory-brain/gateway_health_pull.py` | 每 5 分钟把 `/stats` 拉成按日期归档的健康日志——之后做「哪个模型不稳」周报的数据源。 |
| `memory-brain/router/router.py` | 小 CLI，供 daemon 经网关调模型，自带供应商 fallback。 |
| `watchdog/wd.sh` | 看门狗主循环。读 `services.conf`，SSH 探活，失败拉起，拉起失败进 300 秒冷却。 |
| `watchdog/services.conf.example` | 服务注册表：`名称\|探活命令\|拉起命令\|冷却秒数`。 |
| `windows/probe_*.ps1`、`windows/stop_*.ps1` | Windows 侧 daemon 的探活（exit 0 = 活）与停止脚本。 |
| `docs/PLATFORM.md` | 平台原始设计笔记：状态机、耗掉一天的 stdin bug、与记忆系统的联动规则。 |
| `docs/model-gateway-design.md` | 模型网关设计取舍。 |
| `docs/README-EPR.md` | EPR 设计与自检说明。 |

---

## 快速开始

### 1. 网关（路由器侧）

```sh
mkdir -p /root/model-gateway
cp gateway/gateway.mjs gateway/auto_route.mjs gateway/epr_reduce.mjs /root/model-gateway/
cp gateway/config.example.json /root/model-gateway/config.json   # 然后编辑
node /root/model-gateway/gateway.mjs
```

`config.json` 是唯一必须改的文件。一个渠道 = 一个供应商：

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

一条路由就是一条有序列表：先试第一个，失败了换下一个；一直失败的渠道进冷却，
后续请求直接跳过。

调用方式与任何 OpenAI 端点一致——**路由名就是模型名**：

```sh
curl http://<router-ip>:4100/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"brain","messages":[{"role":"user","content":"你好"}]}'
```

### 2. 监控

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

`by_channel` 回答的是「哪个模型被调了多少次、表现如何」——包括被**切走**
多少次、进了多少次**冷却**。

### 3. 看门狗（路由器侧）

```sh
mkdir -p /root/watch/log
cp watchdog/wd.sh watchdog/gateway_probe.sh /root/watch/
cp watchdog/services.conf.example /root/watch/services.conf   # 然后改目标
crontab -e   # 加一行：* * * * * /root/watch/wd.sh >/dev/null 2>&1
```

### 4. 记忆 daemon（主机侧）

装好 `windows/` 里的探活/停止脚本，给每个 daemon 建计划任务，
再把服务登记进 `services.conf`，把生命周期交给看门狗。
状态机与「新增服务三步走」见 `docs/PLATFORM.md`。

---

## 设计要点（值得知道的坑）

- **冷却，而不是无限重试。** 失败的渠道在 `cooldown_seconds` 内被跳过。
  一家供应商抽风，不会让每个请求都变成超时。
- **自动降级是单向的。** `auto_route.mjs` 只把渠道**往下**挪。恢复靠「日志健康时写空覆盖」，
  被降级的渠道自己会回来。没有任何机制会偷偷把不稳的供应商提回队首。
- **热加载。** `route_overrides.json` 按 mtime 轮询重读，改路由（以及上面的自动降级）
  都不用重启网关。
- **严格解码，拒绝静默污染。** Python 侧对子进程输出做严格 UTF-8 解码，
  解不出来就**报错**，绝不替换字符。而且「UTF-8 解码成功但含 `U+FFFD`」也算失败——
  因为 `U+FFFD` 的 UTF-8 字节恰好是合法 GBK，再解一次会得到看着像人话的垃圾。
- **看门狗必须重定向 stdin。** 在 `while read … < file` 循环里，任何可能读 stdin 的命令
  都得自己重定向，否则会把文件剩下的行吃掉。细节见 `docs/PLATFORM.md`。

---

## 安全

本仓库是**脱敏导出**。供应商 API key、网关 key、局域网地址、主机名与用户名
都已替换为占位符（`<API_KEY>`、`<GATEWAY_KEY>`、`<ROUTER_IP>`、`<USER>`…），
**不含任何可用凭据**。

自己部署时请把网关 key 当作真凭据：它挡在你的 API key 和任何能访问 4100 端口的人之间。
网关请留在局域网内，或在前面加一层带鉴权的反向代理。

---

## 许可

MIT —— 见 [LICENSE](LICENSE)。
