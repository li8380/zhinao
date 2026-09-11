#!/usr/bin/env python3
"""Distill daemon: watch QwenPaw history.db, distill new turns periodically.

Mode: local perpetual process (Windows). A watchdog on the OpenWrt router
SSHes in every minute, checks `pgrep -f distill_daemon`, and relaunches if
dead.

Logic:
- Every POLL_INTERVAL s, check MAX(seq) in history.db
- If new turns arrived and quiescent for QUIET_SECONDS (or first run),
  distill range [cursor+1, max], append entries to today's diary
  '## \u84b8\u998f\u6458\u8981' section (dedup by line), advance cursor
- Cursor file: memory/distill_cursor.md  (last_distilled_seq: N)
"""
import os
import sys
import time
import datetime
import subprocess

# ============================================================
# 编码加固（2026-09-10 事故）：本 daemon 由 schtasks 拉起，任务环境里没有
# PYTHONIOENCODING，任何子进程（含 model-router/router.py）的 stdout 会退化
# 为系统 ANSI 代码页（cp936/GBK），调用方按 UTF-8 解码失败 -> U+FFFD 落盘。
# 本服务当前走 DB 直读不调模型，但统一强制 UTF-8 以防后续扩展踩同一坑。
# ============================================================
os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["PYTHONUTF8"] = "1"

BASE = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
)  # workspace root (scripts -> memory-distillation -> skills -> default)
sys.path.insert(0, os.path.join(BASE, "skills", "memory-distillation", "scripts"))

import distill_db  # noqa: E402

POLL_INTERVAL = 60          # seconds between polls
QUIET_SECONDS = 600         # 10 min without new turns -> distill
CURSOR_FILE = os.path.join(BASE, "memory", "distill_cursor.md")
LOG_FILE = os.path.join(BASE, "scripts", "distill_daemon.log")
MEMORY_DIR = os.path.join(BASE, "memory", "distill")
SECTION = "## 蒸馏摘要"


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


# --- 三遍法则守护 (2026-09-03 维护者决定) ---
# 同类错误连续 3 次 -> 自锁 BLOCKED，写 FAILURE_REPORT 供维护者判断；
# 不退出进程（看门狗探测仍见 alive），进入慢轮询，错误消失自动解除。
MAX_CONSEC_FAILURES = 3
BLOCKED_POLL = 600          # seconds between recovery probes while blocked
BLOCKED_FILE = os.path.join(BASE, "scripts", ".distill_blocked")
FAILURE_REPORT = os.path.join(BASE, "scripts", "distill_failure_report.md")


def write_failure_report(fail_history):
    """fail_history: list of (ts, error_class, message) tuples."""
    os.makedirs(os.path.dirname(FAILURE_REPORT), exist_ok=True)
    lines = [
        "# distill_daemon 故障报告（三遍法则触发）\n",
        f"> 生成：{datetime.datetime.now().isoformat(timespec='seconds')}",
        f"> 规则：同类错误连续 {MAX_CONSEC_FAILURES} 次 → 自锁等待人工判断\n",
        "## 错误时间线\n",
    ]
    for ts, cls, msg in fail_history:
        lines.append(f"- `{ts}` [{cls}] {msg}\n")
    lines += [
        "\n## 建议动作\n",
        "- 维护者查看上方错误明细，判断根因（DB 锁/损坏、游标问题、磁盘满等）\n",
        "- 处理后 daemon 将自动恢复（每 10 分钟轻量探测，错因消失即解除自锁）\n",
        "- 想立即重新尝试：删除 `scripts\\.distill_blocked` 后重启 DistillDaemon 计划任务\n",
    ]
    with open(FAILURE_REPORT, "w", encoding="utf-8") as f:
        f.write("".join(lines))


def blocked_loop(fail_history):
    """BLOCKED state: slow-poll for recovery; auto-unblock when errors clear."""
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
        # recovered
        try:
            os.remove(BLOCKED_FILE)
        except OSError:
            pass
        log("UNBLOCKED: error condition cleared; resuming normal polling")
        return


def read_cursor():
    """Read last_distilled_seq from cursor file. None if missing."""
    if not os.path.exists(CURSOR_FILE):
        return None
    try:
        with open(CURSOR_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("last_distilled_seq:"):
                    return int(line.split(":", 1)[1].strip())
    except Exception as e:
        log(f"cursor read error: {e}")
    return None


def write_cursor(seq):
    os.makedirs(os.path.dirname(CURSOR_FILE), exist_ok=True)
    tmp = CURSOR_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"# Distill cursor - managed by distill_daemon.py\n")
        f.write(f"# last update: {datetime.datetime.now().isoformat(timespec='seconds')}\n")
        f.write(f"last_distilled_seq: {seq}\n")
    os.replace(tmp, CURSOR_FILE)


def append_to_diary(date_str, lines):
    """Append distilled lines to memory/distill/YYYY-MM-DD.md.

    NOTE: memory/YYYY-MM-DD.md is fully managed by the notes:auto background
    task (it rewrites the whole file, wiping manual appends). So distillation
    output goes to memory/distill/YYYY-MM-DD.md instead - still inside the
    memory tree (memory_search sees it) but immune to auto-rewrites.
    Dedups by exact line; returns (added, skipped).
    """
    if not lines:
        return 0, 0
    path = os.path.join(MEMORY_DIR, f"{date_str}.md")
    os.makedirs(MEMORY_DIR, exist_ok=True)

    existing = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            existing = f.read()

    # 按 seq 键去重（在线 judge 已写入的同 seq 条目，批量时自动跳过）
    added_lines, skipped = distill_db.dedup_lines_by_seq(existing, lines)

    if not added_lines:
        return 0, skipped

    head = f"# 蒸馏摘要 {date_str}\n\n<!-- distill:auto ({date_str}) -->\n\n"
    if existing.strip():
        new_block = "\n".join(added_lines) + "\n"
        updated = existing.rstrip() + "\n" + new_block
    else:
        updated = head + "\n".join(added_lines) + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(updated)

    return len(added_lines), skipped


def main():
    log("=== distill_daemon started ===")

    cursor = read_cursor()
    if cursor is None:
        # First run: initialize cursor to yesterday's max seq, so we only
        # distill today's new turns (older history already summarized/handled).
        yest = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        cursor = distill_db.get_last_seq_before(yest)
        write_cursor(cursor)
        log(f"first run: cursor initialized to yesterday max = {cursor}")

    first_pass = True
    last_seen_max = cursor

    # 三遍法则状态
    fail_count = 0
    fail_class = None
    fail_history = []  # (ts, class, message)

    while True:
        try:
            cur_max = distill_db.get_max_seq()
            if cur_max <= cursor:
                first_pass = False
                time.sleep(POLL_INTERVAL)
                continue

            # Some new turns exist. Determine quiescence: use the newest
            # created_at we can get (via max seq row) for silence check.
            quiet = False
            if first_pass:
                quiet = True  # catch up immediately on startup
            else:
                newest_ts = distill_db.get_created_at(cur_max)
                if newest_ts:
                    age = (datetime.datetime.now(datetime.timezone.utc) - newest_ts).total_seconds()
                    quiet = age >= QUIET_SECONDS

            if not quiet:
                time.sleep(POLL_INTERVAL)
                continue

            # Distill the range
            date_str = datetime.date.today().isoformat()
            entries = distill_db.distill_range(cursor, cur_max)
            lines = distill_db.entries_to_lines(entries)
            added, skipped = append_to_diary(date_str, lines)
            log(f"distilled seq {cursor+1}..{cur_max}: {len(entries)} entries "
                f"(added {added}, dup-skipped {skipped}); diary: memory/distill/{date_str}.md")
            write_cursor(cur_max)
            cursor = cur_max
            first_pass = False

            # 乱码自清（2026-09-10 维护者决定：发现乱码直接删，不花 tokens 修复）
            try:
                rm, tf = distill_db.clean_mojibake_lines(MEMORY_DIR)
                if rm:
                    log(f"MOJIBAKE_CLEAN: removed {rm} garbled line(s) from {tf} file(s)")
            except Exception as e:
                log(f"MOJIBAKE_CLEAN failed (non-fatal): {type(e).__name__}: {e}")

            # 成功路径：清零故障计数
            fail_count = 0
            fail_class = None
            fail_history = []

        except Exception as e:
            # 三遍法则：同类错误归组计数，连续 3 次 -> 自锁等维护者判断
            cls = type(e).__name__
            if cls == fail_class:
                fail_count += 1
            else:
                fail_class = cls
                fail_count = 1
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            fail_history.append((ts, cls, str(e))[:3])
            log(f"ERROR#{fail_count} [{cls}]: {e}")
            if fail_count >= MAX_CONSEC_FAILURES:
                blocked_loop(fail_history)
                # unblocked -> reset counters and resume normal polling
                fail_count = 0
                fail_class = None
                fail_history = []

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()