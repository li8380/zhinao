# EPR (Evidence-Preserving Reducer) — 智脑网关落地说明

> 2026-09-11 落地。借鉴 NV Labs SoL-Pi 的 Evidence-Preserving Reducer 机制。
> 一句话：**长输出先交给免费档模型提炼成「摘要 + 证据收据」，只让摘要进上下文；收据可逐条回溯原文行号验证，信息不丢可追溯。**

## 为什么做

- omen-alpha 爆窗教训（167K > 126K 上下文，既不能压缩也不能回滚只能开新会话）
- 长工具输出/长日志直接进上下文会迅速吃满窗口
- 便宜模型先精读长东西（reducer），主模型只看到「验证过的摘要」——SoL-Pi 实测成本省 45-49%

## 架构

```
长文本/长输出
   │
   ▼
epr_reduce.mjs（调用方，本地 or 软路由）
   │  HTTP POST /v1/chat/completions  model="epr"
   ▼
智脑网关（软路由 <ROUTER_IP>:4100，gateway.mjs 不动）
   │  走 epr 路由（全免费链，¥0）
   ▼
免费档模型提炼：openrouter nex-n2.5-pro:free → nex-mini:free
                → nvidia nemotron-super free → modelscope Step-3.7-Flash
                → commandcode ling-free → agnes
   │
   ▼
epr_reduce.mjs 内核做「收据交叉验证」：
   模型声明 [N] 行号X: "片段" → 逐条回溯原文定位真实行号
   → verified（证据找到）/ unverified（模型归纳，宁缺毋滥标记）
   │
   ▼
输出 JSON：{ summary, receipts[], dropped[], ratio, ... }
只把 summary 进上下文，receipts 作为可回溯证据
```

## 核心文件

| 文件 | 位置 | 说明 |
|---|---|---|
| gateway.mjs | 软路由 `/root/model-gateway/` | **未改动**（v1.2 稳定态） |
| config.json | 软路由 `/root/model-gateway/` | 新增 `epr` 路由（全免费链） |
| epr_reduce.mjs | 软路由 + 本地 `deliverables/model-gateway/epr-20260911/` | reducer 调用方 + 收据验证器 |
| 备份 | 软路由 `config.json.bak-epr-20260911` / `gateway.mjs.bak-epr-20260911` | 回滚用 |
| 本地基线 | `deliverables/model-gateway/epr-20260911/` | config.json / gateway.mjs / epr_reduce.mjs / 测试样本 |

## 用法

```bash
# 本地（Windows）
set "GWKEY=sk-gw-<GATEWAY_KEY>"
node epr_reduce.mjs --input <长文本文件> [--max-in 200000] [--chunk 12000]

# 软路由
GWKEY=sk-gw-... node /root/model-gateway/epr_reduce.mjs --input /tmp/x.txt

# 一键自检（回归测试：内置带行号锚点的样本 → 断言验证器回溯率 + 锚点行号）
GWKEY=sk-gw-... node epr_selftest.mjs [--timeout 180]
#   输出 PASS/FAIL + 退出码；结果落 epr_selftest_result.json
#   注意：免费链较慢，单轮自检约 3-5 分钟，建议后台跑

# 输出 JSON 字段
#   summary        提炼后的摘要（多个 CHUNK 用 ---CHUNK--- 分隔）
#   receipts[]     证据收据：{key, line(真实行号), declared_line(模型声明), line_match, snippet}
#   dropped[]      模型声称舍弃的内容 / 未通过回溯的归纳性要点
#   ratio          压缩比（in_chars / out_chars）
#   chunks         分块数
```

## 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--input` | 必填 | 输入文件（避免 shell 引号地狱，铁律） |
| `--gateway` | http://<ROUTER_IP>:4100 | 网关地址 |
| `--route` | epr | 路由名 |
| `--key` / `GWKEY` | 环境变量 | 网关 key |
| `--max-in` | 200000 | 输入上限（超限截断） |
| `--chunk` | 12000 | 分块大小（free 档长文本会超时，分块串行提炼） |
| `--timeout` | 120 | 单次调用超时（s）。**免费档大块会 >120s 挂起，大文件请调小 --chunk 并调大本值** |

## 使用限制（2026-09-11 压测定论）

- **免费链慢是常态，不是「卡死」**：epr 路由是 7 级免费链，遇上游超时会逐级切换（实测一次 30 行小样本网关侧 `switched:8`），端到端耗时数分钟属正常范围。**大块只会更慢**：104KB 样本 3 种块大小（22000/12000/40000）均在观察窗口内未返回。
- **大文件正解**：`--chunk 6000` + `--timeout 300`（单块小 → 单次返回快 → 不撞超时）。
- **不要指望一次大块返回**：free 档（openrouter free / nvidia free / modelscope）延迟抖动大，块越大越容易晾干。
- 完整压测履历见 `PERS_STRESS_TEST-20260911.md`。

## 已验证（2026-09-11）

| 测试 | 结果 |
|---|---|
| epr 路由上线 | ✅ effective_routes 含 epr |
| 小样本 300B | ✅ 链路通，free 档正常提炼 |
| 日志样本 12KB | ✅ 压缩 21.9x（12000→549 字符），3/5 收据回溯命中 |
| 收据交叉验证 | ✅ **抓到模型行号错误**（声明 line5 实际 line1）+ 归纳性内容正确标 unverified |
| 分块机制 | ✅ 12KB/4000 → 4 块串行，全局行号偏移正确 |
| 路由生效 L2 验收 | ✅ 网关日志 `route 'epr' -> openrouter (nex-agi/nex-n2.5-pro:free) ok` + 响应体 `"cost":0` + stats `openrouter 8→10` |
| 自检验收（修复前后） | ✅ **假阴性 67% → 0%**：修复前 4/12 verified（8 条误判 unverified）→ 修复后 **12/12 verified，全部 `line_match:true`**；一键复现 `node epr_selftest.mjs` |
| 大样本 104KB（1081 行） | ⚠️ **已定论（根因 2026-09-11 修正）**：不是「块太大卡死」，是**免费链逐级试探本身就慢**（434B/1 块也需数分钟；`switched` 计数高）。正解=小块 + 长超时 + 后台轮询；见 `PERS_STRESS_TEST-20260911.md` |

## 回滚

```bash
# 软路由
cd /root/model-gateway
cp config.json.bak-epr-20260911 config.json   # 移除 epr 路由
sh restart_gateway.sh
# epr_reduce.mjs 删除（保留文件也行，无人调用无副作用）
```

## 经验教训

1. **提示词约定的输出格式不可靠**——同一模型两次输出 `行号X: "片段"` / `→ line N` 两种格式；验证器必须双格式兼容 + 内核独立回溯，不能赌模型听话
2. **模型会编行号**——小样本原文只有 1 行，模型声明行号 2/3/4；EPR 的价值正是内核校验抓出这类幻觉
3. **归纳 ≠ 摘录**——`rows=7`（每行+7 的规律）是模型总结，不是原文短语；回溯不到要标 unverified 而非静默通过（宁缺毋滥）
4. **免费链慢是常态（根因修正 2026-09-11）**——epr 走 7 级免费链，遇上游超时逐级切换（实测小样本 `switched:8`），端到端数分钟属正常，**不是卡死**。旧结论「块太大卡死」已被反证（434B/1 块同样慢）。**安全参数：`--chunk 6000-12000` + `--timeout 300` + 后台运行轮询**
6. **验证器短关键词假阴性 bug（2026-09-11 自检发现并修复）**——旧版匹配窗口下限硬编码 6，而中文关键词常 3-5 字 → 循环条件直接为假 → 跳过匹配误判 unverified（实测 12 条收据 8 条假阴性 = 67%）。修复：新增 `locatePhrase()` 归一化逐行回溯（空格口径两侧统一）+ 下限降到 3。一键复验：`node epr_selftest.mjs`
5. cmd 引号坑——远程命令带 `|` 时用文件重定向避免 cmd 提前解析（AGENTS.md 铁律 3）

## 后续可选

- 接入蒸馏流程：judge/distill 的超长原始对话先用 EPR 提炼再进记忆（省 token）
- B 档「能力地板抽检」：从 distill 抽记忆 → 回溯 history.db 验证（EPR 收据行号可复用）
- 分块可并行（当前串行，4 块 ≈ 4 次 free 档响应时间）
- **一键自检**：`node epr_selftest.mjs`（内置带行号锚点的样本，断言回溯率 + MARKER 锚点精确到行）。2026-09-11 实测 **ALL PASS**：91s / 6 收据 / 回溯 100% / MARKER→L11 精确命中。
- 压测完整记录见 `PERS_STRESS_TEST-20260911.md`（5 次尝试履历 + 网关 stats 证据 + 根因修正）
