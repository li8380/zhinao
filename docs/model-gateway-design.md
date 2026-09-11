# 模型网关方案（Model Gateway）— 底层大脑 + 干活模型故障切换

> 日期：2026-09-09 ｜ 状态：方案待拍板 ｜ 归属：🤖 大模型区落地
> 一句话：在软路由上架一个 OpenAI 兼容网关，聚合 GOAT/硅基/Agnes 多路上游，对外暴露单一 API。QwenPaw 只需新增一个 provider 指向网关，由网关负责模型路由与失败切换——"底层大脑指挥干活模型，干活的挂了自动换手"。

---

## 1. 背景与痛点

1. **QwenPaw 一次只能选一个模型**：实测本地配置 `llm_retry_enabled=true / llm_max_retries=3`，但重试只打同一个模型，**没有跨模型失败切换**。
2. **真实事故**：2026-09-09 deepseek-v4-flash 报 `openai.APIConnectionError`（网络层故障）→ 当时全链路卡死。
3. **DPH 启示**：9-09 DPH 原理拆解结论——"模型应像插槽里的卡，可热插拔"。本方案把该哲学落地到生产体系（先静态切换，二期再做 LLM 动态编排大脑）。

## 2. 事实侦察（2026-09-09 实测）

| 项目 | 实测结果 | 影响 |
|---|---|---|
| 软路由 | ImmortalWrt 25.12.1 r37978 x86_64 @ <ROUTER_IP> | 部署位 |
| Node.js | v22.23.2 ✅（/usr/bin/node） | Node 方案零安装 |
| Python | ❌ 无 | LiteLLM 不优先 |
| Docker | ❌ 无 | 容器方案需先装 Docker，否决 |
| 内存 | 8GB，可用 7.3GB | 充裕 |
| 磁盘 | `/` 剩 2.7GB；/mnt/sda3 24.8GB 已挂载 | 脚本/日志放 sda3 |
| QwenPaw | 42 providers、7 custom（agnes/meituan/nvidia/opencode-custom/sensenova/tokenrhythm/volcengine-new），全是 OpenAI 兼容 | 接入 = 加第 8 个 provider |

## 3. 引擎选型（基于侦察修订）

| 引擎 | 语言 | 软路由可行性 | 失败转移 | 维护成本 | 结论 |
|---|---|---|---|---|---|
| LiteLLM | Python | ❌ 无 Python，pip 生态风险高 | ✅ 原生 fallback | 中 | 否决（仅作本地 Windows 备选） |
| new-api / one-api | Go 单二进制 | ✅ x86_64 下载即跑 | ✅ 渠道重试/切换 | 中高（带管理后台） | 候选 B |
| **自研 Node 网关** | Node v22 | ✅ 现成 + fetch 内置 | 自写（完全可控） | 低 | **候选 A（推荐）** |

推荐 A 理由：零安装依赖、代码全可控（切换策略可定制）、体积几百 KB、和看门狗平台天然契合；new-api 功能全但重（用户体系/计费/管理界面，个人场景杀鸡用牛刀）。

## 4. 总体架构（分两期）

```
[一期 · 静态故障转移网关]                      [二期 · LLM 编排大脑（预留）]
┌─────────────────────────────┐        ┌──────────────────────────────┐
│  QwenPaw（客户端零改造）       │        │  底层大脑模型 orchestrate    │
│  provider = gateway          │        │  （按任务动态决策用哪个模型）   │
└──────────┬──────────────────┘        └──────────────┬───────────────┘
           │ OpenAI 兼容 http://<ROUTER_IP>:4100/v1  │
┌──────────▼──────────────────┐        │               │
│  网关服务（Node, 软路由常驻）  │◄───────┘   二期把"决策"交给 LLM：
│  路由策略 + 失败切换 + 健康检查 │          一期是静态 fallback 链；
│  日志 + 看门狗接入             │          二期是动态编排。
└──────┬───────┬───────┬──────┘
       ▼       ▼       ▼
   GOAT(主力) 硅基(兜底1) Agnes(兜底2·免费)
  deepseek-v4-flash / glm-5.2 / mimo 等
```

## 5. 网关核心逻辑设计

### 5.1 OpenAI 兼容入口
- `POST /v1/chat/completions`（含 streaming 转发）
- 透传 messages/tools/temperature；模型名映射（`main` → 实际上游）

### 5.2 失败分类与切换规则（核心）

| 上游返回 | 判定 | 动作 |
|---|---|---|
| `APIConnectionError` / 网络超时 | 网络层故障 | 切下一优先级，重试 |
| HTTP 429 / 5xx | 限流 / 服务端挂 | 切下一优先级 |
| HTTP 401 | 鉴权错误 | **不切**（切了也白切）→ 记 ERROR + 告警 |
| HTTP 400（超长上下文除外） | 请求错误 | 不切 → 原样透传 400 |
| 无限期挂起 | 僵尸 | 主动健康探针兜底（改自看门狗 probe 模式） |

- 每次失败进入 `cooldown`（冷却 300s，参考看门狗平台设计），避免反复击打坏渠道
- 全链路失败 → 返回 503 + 错误详情（可含各渠道失败原因）

### 5.3 兜底红线
备用链全挂时，**网关最后一道 = Agnes 免费模型**（或已购的硅基 paid 模型），保证"服务不断、质量降级"。二期由底层大脑自行顶上回答。

### 5.4 健康检查
- 被动：每次请求失败自动标记
- 主动：cron 每分钟 `probe_gateway.sh`（复用 wd.sh 模式）探测各上游最小成本模型（glm-4.7-flash / qwen3.5-4b 等免费档），失败进冷却

### 5.5 日志与告警
- 日志：`/mnt/sda3/logs/gateway.log`（分级 INFO/ERROR，只记切换与异常，安静待命）
- 告警：切换发生时写一行 `CHANGE old→new reason=xxx`，由看门狗框架统一管理（如需主动通知再挂 Telegram/ServerChan）

## 6. 上游渠道接入表

| 渠道 | 优先级 | 候选模型 | 成本（已有） | key 来源 |
|---|---|---|---|---|
| GOAT（Command Code） | 主力 | deepseek-v4-flash（编码）/ glm-5.2（中文）/ mimo-v2.5（低价） | $10/月已购 | 供应商后台 Studio key |
| 硅基流动 | 兜底 1 | deepseek-v4-flash / qwen3.5-4b（免费档） | ¥44.5/月已购 | 现有 provider key |
| Agnes | 兜底 2 | agnes-2.0-flash | 永久免费 | 现有 provider key |
| 火山方舟（可选） | 兜底 3 | doubao 系列 | 已充值 | 现有 provider key |

> 模型名映射例子：`main` → `goat/deepseek/deepseek-v4-flash`（GOAT 模型名带前缀）；`main2` → `siliconflow/deepseek-ai/DeepSeek-V3` 等，可配多个别名，按任务选。

## 7. QwenPaw 接入（客户端零改造）

新增一个 provider（与 agnes/volcengine-new 同构）：

```json
{
  "id": "gateway",
  "base_url": "http://<ROUTER_IP>:4100/v1",
  "chat_model": "OpenAIChatModel",
  "is_custom": true,
  "api_key": "<API_KEY>",
  "models": ["main", "main2"]
}
```

当前会话 active_model 选 `gateway/main`。回滚 = 切回原 provider（如 opencode-custom），配置不动。

## 8. 部署与看门狗（复用智脑平台）

```
软路由 /root/model-gateway/
  ├── gateway.mjs        # 网关主程序（Node 零依赖）
  ├── config.json        # 渠道表/切换策略/冷却参数
  ├── probe_gateway.sh   # 存活+上游健康探测
  └── logs/ → /mnt/sda3/logs/gateway.log
```

- 注册：`services.conf` 加一行（看门狗 wd.sh 每分钟探活，挂了一分钟内拉起）——与 distill/judge 同款机制，模板 `scripts/services_template/` 现成
- init：将 gateway 加为 init 服务，软路由重启自动恢复
- 端口：4100（内网专用，防火墙仅允许 LAN 访问；无公网暴露）

## 9. 实施步骤（分阶段，每阶段有验收）

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0 | 备份软路由现有配置（/root/backup-*），准备回滚 | 备份文件存在 |
| P1 | 写 gateway.mjs 雏形，本地 Windows 直测（直连各上游） | 单渠道 curl 通 |
| P2 | 上软路由 + init + services.conf + probe | 断一个渠道验证自动切换 |
| P3 | QwenPaw 新增 gateway provider + 当前会话切换 | QwenPaw 对话走网关 |
| P4 | 故障演练：停 GOAT → 自动切硅基 → 恢复后回主 | 切换日志 CHANGE 可见 |
| P5 | 沉淀：写技能/更新 ASSET_INVENTORY | 文档入册 |

## 10. 成本核算

| 项 | 金额 |
|---|---|
| 软路由硬件 | 已有（¥0） |
| 网关引擎 | 自研 Node，开源免费（¥0） |
| 上游订阅 | GOAT $10 + 硅基 ¥44.5（已购，复用） |
| Agnes 备用 | 免费 |
| **新增成本** | **¥0**（唯一成本：部署时间 P1-P4 ≈ 半天） |

## 11. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 网关自身宕机 | 看门狗 1 分钟拉起；极端情况 QwenPaw 切回原 provider（配置没删） |
| 切换误判（把暂时的抖动当宕机） | 冷却 + 重试次数可调；保守策略：明确错误才切 |
| 软路由重启 | init 服务自动恢复 |
| 上游 key 变动 | 改 config.json 一处生效 |
| 网关被内网滥用 | 仅 LAN 监听 + 自设 key；如需加密走内网已够 |

## 12. 待拍板决策点

1. **引擎**：A 自研 Node 网关（推荐） vs B new-api（Go 二进制）？
2. **部署位**：软路由 7×24（推荐）确认？
3. **兜底红线**：全挂时降级到 Agnes 免费（推荐）还是宁可报错等恢复？
4. **通知**：切换时静默记日志（推荐）还是主动推送（需选通知渠道）？
5. **二期**：LLM 编排大脑（底层模型动态决策）是否作为后续迭代预留？

---
*关联：本方案与 2026-09-09「QwenPaw vs DPH 原理对比」结论一致——不迁移 DPH，把"模型热插拔"哲学落地为生产资产。*