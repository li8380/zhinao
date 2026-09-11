# 常驻服务平台（Watchdog Platform）

> Windows 常驻引擎 + X86 软路由（ImmortalWrt <ROUTER_IP>）7×24 看门狗
> 成立：2026-09-03（服务 #1 = memory-distillation 蒸馏 daemon）
> 机制起源：维护者要求——后续所有功能都按这套机制做
>
> **定位（2026-09-03 维护者定名）**：项目正式名 = **智脑**（常驻记忆中枢，英文代号 ZhiNao）。
> 本平台是「智脑」的 **⑤ 保障层**——系统本体 = 感知(distill_daemon 随时采集) → 提炼(蒸馏引擎+judge 语义鉴定)
> → 存储(蒸馏归档/日记/MEMORY.md) → 读取(memory_search/recall_history 回溯)。
> 看门狗只负责保证这些层 7×24 活着，不是系统本身。

## 架构总览

```
┌─ Windows (<WINDOWS_HOST_IP>) ────────────────────────────────┐
│  scripts/<svc>_daemon.py   业务常驻进程（轮询+静默+游标）  │
│  scripts/probe_<svc>.ps1   探测器（exit 0=活 / 1=死）       │
│  scripts/stop_<svc>.ps1    停止器（演练/手动恢复）          │
│  计划任务 <Svc>            拉起通道（schtasks 托管，        │
│                            SSH 会话结束进程不灭）           │
│  sshd 服务                 Automatic（开机自启）           │
└──────────────────────────────┬─────────────────────────────┘
                               │ ssh 免密（dropbear 密钥 id_distill）
┌─ 路由器 /root/watch/ ────────▼─────────────────────────────┐
│  services.conf   服务注册表：svc|probe_cmd|relaunch_cmd|冷却 │
│  wd.sh           统一看门狗（cron 每分钟跑）                 │
│  log/<svc>.log   分级日志（INFO 恢复 / ERROR 失败）          │
│  .watch_retry_<svc>  冷却标记（失败后 5 分钟再试）           │
└────────────────────────────────────────────────────────────┘
```

## 状态机（每服务）

| 状态 | 行为 | 日志 |
|---|---|---|
| 探测活着 | 静默 | 无 |
| 死了 + 拉起成功 | 记录恢复 | INFO |
| 死了 + 拉起失败 | 记录失败 + 冷却标记，5 分钟内不再狂试 | ERROR |
| 冷却到期 | 自动再试 | 成功 INFO / 失败 ERROR |

## 为什么这套机制

- **1 分钟缓冲**（维护者决定）：开机集中拉起怕崩，cron 每分钟探测天然缓冲
- **冗余计划**：失败进 300s 冷却后自动重试，不放弃也不打扰
- **报错级日志**：只有异常写 ERROR，正常恢复写 INFO，巡检一眼看清
- **零 LLM 成本**：业务引擎（如蒸馏）纯规则，不依赖任何模型

## 新增服务

见 `scripts/services_template/README.md`——三步：写 daemon → 建 probe/stop → 注册 conf + 计划任务。基础设施（wd.sh/冷却/日志/SSH 链路/crontab）全套复用，零重复建设。

## 已注册服务

| # | 服务 | daemon | probe | 说明 |
|---|---|---|---|---|
| 1 | distill 蒸馏 | `skills/memory-distillation/scripts/distill_daemon.py` | `probe_distill.ps1` | 批量蒸馏：history.db → memory/distill/（范围行） |
| 2 | judge 语义鉴定 | `scripts/judge_daemon.py` | `probe_judge.ps1` | 逐条语义鉴定：用户事实单行（语义版优先） |
| 3 | profile 画像维护 | `scripts/profile_daemon.py` | `probe_profile.ps1` | 画像信号句 → LLM 增量 ops → memory/profile_state.json + PROFILE.md（EverOS 规则复刻，2026-09-04） |

> **服务分工**：distill = 批量主题合并（决策/产出/纠正…范围行）；judge = 逐条语义鉴定（用户个人事实/状态/健康/偏好/决策单行）；profile = 画像信号预筛 → LLM 增量更新结构化画像（显式信息 + 隐式特征）。独立游标（`memory/distill_cursor.md` / `memory/judge_cursor.md` / `memory/profile_cursor.md`），dedup 按粒度分离，互不阻塞。

## ⚠️ 看门狗 stdin bug（2026-09-03 修复，服务 #2 上线时暴露）

**现象**：追加 judge 服务后看门狗不处理它，`services.conf` 内容/换行/CRLF 排查全无效。

**根因**：`wd.sh` 循环 `done < "$CONF"` 重定向 stdin，但循环体内 ssh 调用只重定向了 stdout/stderr——**ssh 会消费继承的 stdin（文件描述符）**，把 services.conf 剩余行当 stdin 转发到远程后丢弃，导致 while read 下次直接 EOF。distill 是唯一服务（最后一行）时被掩盖；第二个服务行必被吞。

**修复**：所有 ssh 调用加 `< /dev/null`（probe ×2、relaunch ×1）。

**经验**：凡在 `while read ... < file` 循环内调用任何可能读 stdin 的命令，必须显式重定向 stdin。

## 与 MEMORY 联动（2026-09-03 维护者要求：看门狗必须和记忆系统联动）

**原则：状态翻转才写记忆，平常静默零污染。**

| 联动 | 实现 |
|---|---|
| ① 事件沉淀 | wd.sh `ev()` 在状态翻转时（RELINKED 拉起成功 / FAILED 拉起失败 / RECOVERED 死后自愈）通过反向 ssh 往 Windows `memory/watchdog-events-YYYY-MM-DD.md` append 一行 `- HH:MM:SS \| svc \| 状态 \| 详情`。链路断时写失败宽容忽略（`|| true`），恢复后 RECOVERED 补给状态 |
| ② 服务表同步 | 新增服务流程加一步：同步 MEMORY.md「Watchdog Platform 常驻服务」表 + services.conf（见 services_template/README） |
| ③ 状态速查 | MEMORY.md 平台段含速查命令（probe ps1 / 路由器 tail 日志），助手问答"平台活着吗"直接拉状态 |

**事件文件**：`memory/watchdog-events-YYYY-MM-DD.md`（当天一个文件，日归档）
**事件格式**：`- 2026-09-03 16:00:00 | distill | RELINKED | relaunch OK`
**读取方**：助手每日日记/复盘时扫一眼即知平台健康史；蒸馏/记忆中枢不消费该文件（避免循环依赖）

## 故障排查

| 症状 | 排查路径 |
|---|---|
| 服务 1 分钟内不恢复 | 路由器 `cat /root/watch/log/<svc>.log` 看 ERROR 详情 |
| ERROR 反复出现 | Windows 未登录？`schtasks /query /tn <Svc>` 任务状态？sshd 活着？ |
| 看门狗整体不跑 | 路由器 `pgrep crond`；`cat /etc/crontabs/root` 应有 wd.sh 行 |
| 追加服务后不生效 | `services.conf` 尾部务必有换行（wd.sh 兼容无尾换行但推荐完整换行） |
| Windows 重启后 | sshd 自启 + 登录后 1 分钟看门狗拉起，游标补齐断档 |

## 关键文件清单（备份/回滚参考）

- 路由器：`/root/watch/wd.sh`、`/root/watch/services.conf`、`/root/watch/log/`、`/root/distill_watch.sh.bak.20260903`（v2 版单服务看门狗备份）
- Windows：`scripts/watch/wd.sh`、`scripts/watch/services.conf`（源文件）、`scripts/services_template/`（模板）、`scripts/probe_distill.ps1`、`scripts/stop_distill.ps1`
- 蒸馏业务：`skills/memory-distillation/scripts/distill_db.py`（引擎）、`distill_daemon.py`（守护）、`memory/distill_cursor.md`（游标）、`memory/distill/`（结果）