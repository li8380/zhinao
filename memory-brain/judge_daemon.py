#!/usr/bin/env python3
"""Judge daemon: 记忆中枢①语义鉴定常驻（Watchdog Platform 服务 #2，2026-09-03）。

轮询 QwenPaw history.db 的**新用户 turn**（kind='context_msg' role='user'，
游标 > judge_cursor），逐条过语义鉴定（judge_turn：规则预筛 -> model-router
免费链 agnes/modelscope -> keep 写 memory/distill/，语义版优先覆盖旧单行）。

与 distill daemon 的关系：
- distill（服务 #1）= 批量蒸馏：按游标合并主题范围行（决策/产出/纠正…）
- judge（服务 #2）= 逐条语义鉴定：抓用户个人事实/状态/健康/偏好/决策单行
- 两者独立游标、互不阻塞（dedup 按粒度分离：单行 vs 单行、范围行 vs 范围行）

看门狗：路由器 /root/watch/wd.sh 每分钟 SSH 探测 probe_judge.ps1，死了
schtasks /run /tn JudgeDaemon 拉起（pythonw 无窗口后台）。

三遍法则：DB 层错误（同类连续 3 次）-> 自锁 BLOCKED 写故障报告慢轮询；
模型链路错误交给 judge 模块自身降级（.judge_pure_rule），daemon 不锁。
"""
import os
import sys
import time
import datetime
import sqlite3

# ============================================================
# 编码加固（2026-09-10 事故）：本 daemon 由 schtasks 拉起，任务环境里没有
# PYTHONIOENCODING，子进程（model-router/router.py）的 stdout 会退化为系统
# ANSI 代码页（cp936/GBK）；调用方按 UTF-8 解码失败 -> 中文变 U+FFFD 落盘。
# 在进程级**强制** UTF-8（不用 setdefault：错的值也要覆盖），
# 使本进程后续创建的所有 subprocess 继承正确编码。
# ============================================================
os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["PYTHONUTF8"] = "1"

BASE = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)  # workspace root：scripts/ -> default（注意：judge_daemon.py 在 scripts/ 下，仅需一次 ..；distill_daemon 在 skills/*/scripts/ 下需三次 ..）
sys.path.insert(0, os.path.join(BASE, "skills", "memory-distillation", "scripts"))

import distill_db  # noqa: E402
import judge  # noqa: E402

POLL_INTERVAL = 60           # seconds between polls
MAX_PER_CYCLE = 10           # max turns judged per poll cycle (模型调用较慢)
CURSOR_FILE = os.path.join(BASE, "memory", "judge_cursor.md")
LOG_FILE = os.path.join(BASE, "scripts", "judge_daemon.log")
HISTORY_DB = distill_db.HISTORY_DB

# --- 三遍法则守护（对齐 distill_daemon）---
MAX_CONSEC_FAILURES = 3
BLOCKED_POLL = 600           # seconds between recovery probes while blocked
BLOCKED_FILE = os.path.join(BASE, "scripts", ".judge_blocked")
FAILURE_REPORT = os.path.join(BASE, "scripts", "judge_failure_report.md")


def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass  # pythonw.exe mode: no console, print is None; log file only
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def write_failure_report(fail_history):
    os.makedirs(os.path.dirname(FAILURE_REPORT), exist_ok=True)
    lines = [
        "# judge_daemon 故障报告（三遍法则触发）\n",
        f"> 生成：{datetime.datetime.now().isoformat(timespec='seconds')}",
        f"> 规则：同类错误连续 {MAX_CONSEC_FAILURES} 次 → 自锁等待人工判断\n",
        "## 错误时间线\n",
    ]
    for ts, cls, msg in fail_history:
        lines.append(f"- `{ts}` [{cls}] {msg}\n")
    lines += [
        "\n## 建议动作\n",
        "- 先生查看上方错误明细，判断根因（DB 锁/损坏、游标问题、磁盘满等）\n",
        "- 处理后 daemon 将自动恢复（每 10 分钟轻量探测，错因消失即解除自锁）\n",
        "- 想立即重新尝试：删除 `scripts\\.judge_blocked` 后重启 JudgeDaemon 计划任务\n",
    ]
    with open(FAILURE_REPORT, "w", encoding="utf-8") as f:
        f.write("".join(lines))


def blocked_loop(fail_history):
    write_failure_report(fail_history)
    with open(BLOCKED_FILE, "w", encoding="utf-8") as f:
        f.write(datetime.datetime.now().isoformat(timespec="seconds") + "\n")
    log(f"BLOCKED: {MAX_CONSEC_FAILURES} consecutive same-class failures; "
        f"report -> {FAILURE_REPORT}; waiting for recovery/human review")
    while True:
        time.sleep(BLOCKED_POLL)
        try:
            distill_db.get_max_seq()  # lightweight probe
        except Exception:
            continue
        try:
            os.remove(BLOCKED_FILE)
        except OSError:
            pass
        log("UNBLOCKED: error condition cleared; resuming normal polling")
        return


def read_cursor():
    if not os.path.exists(CURSOR_FILE):
        return None
    try:
        with open(CURSOR_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("last_judged_seq:"):
                    return int(line.split(":", 1)[1].strip())
    except Exception as e:
        log(f"cursor read error: {e}")
    return None


def write_cursor(seq):
    os.makedirs(os.path.dirname(CURSOR_FILE), exist_ok=True)
    tmp = CURSOR_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("# Judge cursor - managed by judge_daemon.py\n")
        f.write(f"# last update: {datetime.datetime.now().isoformat(timespec='seconds')}\n")
        f.write(f"last_judged_seq: {seq}\n")
    os.replace(tmp, CURSOR_FILE)


def next_user_turn(gt):
    """Oldest user turn with seq > gt. Returns (seq, text) or None."""
    conn = sqlite3.connect(HISTORY_DB, timeout=5)
    try:
        row = conn.execute(
            "SELECT seq, content FROM conversation_history "
            "WHERE kind='context_msg' AND role='user' AND seq > ? "
            "ORDER BY seq ASC LIMIT 1", (gt,)).fetchone()
        return (row[0], row[1] or "") if row else None
    finally:
        conn.close()


def process_turn(seq, text):
    """Judge one user turn -> write keep entry. Returns result dict."""
    # 2026-09-04 guard: skip transient mojibake (concurrent mid-write snapshot).
    # Same guard as distill_db.py::process_turn. Cursor still advances (run_cycle
    # finally always writes seq) so we never wedge on a garbage row.
    if "\ufffd" in text:
        log(f"turn {seq}: SKIP mojibake (concurrent write snapshot)")
        return {"keep": False, "reason": "mojibake", "mode": "rule"}
    result = judge.judge_turn(text, kind="user")
    keep = result.get("keep", False)
    if not keep:
        log(f"turn {seq}: not keep ({result.get('reason') or result.get('mode')})")
        return result
    # 语义版优先：模型判定覆盖旧单行；规则兜底先写者胜（保持与 CLI 一致）
    overwrite_ok = result["mode"].startswith("model")
    added, skipped, replaced, path, rule_added = judge.persist(
        seq, result["category"], result["summary"], overwrite=overwrite_ok)
    log(f"turn {seq}: KEEP [{result['category']}] {result['summary']} "
        f"(mode={result['mode']}, added={added}, replaced={replaced}, "
        f"dup_skipped={skipped}, rule_dual_write={rule_added})")
    result.update({"added": added, "replaced": replaced, "dup_skipped": skipped})
    return result


def run_cycle(cursor):
    """Judge up to MAX_PER_CYCLE new user turns. Returns new cursor."""
    n = 0
    while n < MAX_PER_CYCLE:
        turn = next_user_turn(cursor)
        if turn is None:
            break
        seq, text = turn
        try:
            process_turn(seq, text)
        finally:
            cursor = seq  # 游标始终推进：预筛/判定/写失败都不回退（幂等由是否 keep 决定）
            write_cursor(seq)
        n += 1
    if n:
        log(f"cycle: judged {n} new turn(s), cursor -> {cursor}")
    # 乱码自清（2026-09-10 先生拍板：发现乱码直接删，不花 tokens 修复）
    # 每轮都跑（不只 n>0）：兜底覆盖任何来源的残留乱码；7 个小文件读取开销可忽略
    try:
        rm, tf = distill_db.clean_mojibake_lines(judge.MEMORY_DIR)
        if rm:
            log(f"MOJIBAKE_CLEAN: removed {rm} garbled line(s) from {tf} file(s)")
    except Exception as e:
        log(f"MOJIBAKE_CLEAN failed (non-fatal): {type(e).__name__}: {e}")
    return cursor


def main():
    log("=== judge_daemon started ===")

    cursor = read_cursor()
    if cursor is None:
        # First run: 从当前 max seq 开始（历史对话已由 distill/手动测试覆盖，
        # 常驻只处理“从现在起”的新用户 turn）
        cursor = distill_db.get_max_seq()
        write_cursor(cursor)
        log(f"first run: cursor initialized to current max = {cursor}")

    # 三遍法则状态（DB 层错误）
    fail_count = 0
    fail_class = None
    fail_history = []

    while True:
        try:
            cursor = run_cycle(cursor)
            fail_count = 0
            fail_class = None
            fail_history = []
        except Exception as e:
            cls = type(e).__name__
            if cls == fail_class:
                fail_count += 1
            else:
                fail_class = cls
                fail_count = 1
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            fail_history.append((ts, cls, str(e)))
            log(f"ERROR#{fail_count} [{cls}]: {e}")
            if fail_count >= MAX_CONSEC_FAILURES:
                blocked_loop(fail_history)
                fail_count = 0
                fail_class = None
                fail_history = []
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()