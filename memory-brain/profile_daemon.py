#!/usr/bin/env python3
"""Profile daemon: 智脑结构化画像维护（服务 #3，2026-09-04）。

参考 EverOS 画像规则（ProfileExtractor）实现自动化画像：
- 轮询 QwenPaw history.db 的新**用户 turn**（kind='context_msg' role='user'，
  游标 > profile_cursor），规则预筛画像信号句（"我是/我在/我偏好/我决定/
  我习惯/我负责…" 等）-> 候选走 model-router 免费模型做**增量 UPDATE**
  （LLM 输出 operations: add/update/delete/none）-> 应用后写回结构化画像。
- 画像存储：memory/profile_state.json（机器权威状态）+ 同步渲染到
  PROFILE.md「结构化画像」段（人类可读视图）。
- 模式：INIT（无画像）全量提取；UPDATE（有画像）增量 operations；
  条目数超 MAX_ITEMS 上限时 LLM compact 压缩（合并同类/删过时）。

与 distill/judge 关系：三服务独立游标互不阻塞；本服务只吃「画像信号」，
不抢 distill（决策/产出）与 judge（用户事实）的条目。

看门狗：路由器 /root/watch/wd.sh 每分钟 SSH 探测 probe_profile.ps1，
死了 schtasks /run /tn ProfileDaemon 拉起（pythonw 无窗口后台）。
三遍法则：DB 层/模型链路同类错误连续 3 次 -> 自锁 BLOCKED 写故障报告
慢轮询；模型恢复探活后自动解除。
"""
import os
import sys
import re
import json
import time
import datetime
import sqlite3
import subprocess

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
)  # workspace root（scripts/ 仅需一次 ..）
sys.path.insert(0, os.path.join(BASE, "skills", "memory-distillation", "scripts"))
sys.path.insert(0, os.path.join(BASE, "skills", "model-router"))

import distill_db  # noqa: E402
import judge as judge_mod  # noqa: E402  （复用其显式解码：UTF-8 优先 / GBK 兜底 / 损坏即失败）

POLL_INTERVAL = 60          # seconds between polls
MAX_PER_CYCLE = 3           # max profile updates per cycle (模型较慢,克制)
CURSOR_FILE = os.path.join(BASE, "memory", "profile_cursor.md")
LOG_FILE = os.path.join(BASE, "scripts", "profile_daemon.log")
HISTORY_DB = distill_db.HISTORY_DB
ROUTER = os.path.join(BASE, "skills", "model-router", "router.py")

STATE_FILE = os.path.join(BASE, "memory", "profile_state.json")   # 机器权威
PROFILE_MD = os.path.join(BASE, "PROFILE.md")                     # 人类可读渲染
MAX_ITEMS = 30                # explicit+implicit 合计上限
COMPACT_THRESHOLD = 45        # 超过触发 compact

MODEL_PROVIDERS = ["gateway", "agnes", "modelscope"]  # 网关优先（软路由 simple 链），失败落 Agnes→ModelScope 直连兜底
MODEL_TIMEOUT = 30
MAX_CONSEC_ERRORS = 3
BLOCKED_POLL = 600
BLOCKED_FILE = os.path.join(BASE, "scripts", ".profile_blocked")
FAILURE_REPORT = os.path.join(BASE, "scripts", "profile_failure_report.md")

# 画像信号词：命中才触发画像更新（省 token 预筛）
SIGNAL_RE = re.compile(
    r"(我是|我叫|我在|我住在|我来自|我老家|我是做|我负责|我担任|我从事|"
    r"我偏好|我偏爱|我喜欢|我讨厌|我反感|我习惯|我决定|我选择|我主张|"
    r"我的原则|我的方法|我通常|我倾向|我不再|我已经|我今年|我出生|"
    r"我擅长|我不擅长|我怕|我焦虑|我有病|我健康|我的性格|我这个人)"
)


def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def write_cursor(seq):
    tmp = CURSOR_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("# Profile cursor - managed by profile_daemon.py\n")
        f.write(f"# last update: {datetime.datetime.now().isoformat(timespec='seconds')}\n")
        f.write(f"last_profile_seq: {seq}\n")
    os.replace(tmp, CURSOR_FILE)


def read_cursor():
    try:
        with open(CURSOR_FILE, "r", encoding="utf-8") as f:
            for line in f:
                m = re.match(r"last_profile_seq:\s*(\d+)", line)
                if m:
                    return int(m.group(1))
    except Exception:
        pass
    return 0


# ---------- 画像状态读写 ----------

def load_state():
    """Return dict {explicit_info: [...], implicit_traits: [...], updated_at}."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            log("STATE corrupt -> re-init from PROFILE.md")
    # INIT from current PROFILE.md render (best effort) then daemon will
    # fill via INIT extraction on first signal turn.
    return {"explicit_info": [], "implicit_traits": [], "updated_at": ""}


def _scrub_mojibake(state):
    """落盘自检（2026-09-10 铁律第 5 条）：丢弃含 U+FFFD 的画像条目。

    宁可少一条，也绝不把乱码写进 PROFILE.md / profile_state.json
    （U+FFFD 不可逆；且乱码还会诱导模型编造虚假条目——本次事故已实证）。
    """
    dropped = 0
    for key in ("explicit_info", "implicit_traits"):
        items = state.get(key) or []
        kept = [it for it in items if "\ufffd" not in json.dumps(it, ensure_ascii=False)]
        dropped += len(items) - len(kept)
        state[key] = kept
    if dropped:
        log(f"SCRUB: dropped {dropped} mojibake (U+FFFD) profile entr(ies) before write")
    return state


def save_state(state):
    _scrub_mojibake(state)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def _clean_lines(text):
    return [l.rstrip() for l in text.splitlines()]


def render_profile_md(state):
    """Render human-readable profile section text (pure markdown lines)."""
    lines = []
    lines.append("### 显式信息（explicit_info）")
    exp = state.get("explicit_info") or []
    if not exp:
        lines.append("（暂无 — 等待画像信号自动沉淀）")
    for it in exp:
        lines.append(f"- **{it.get('category','未分类')}**")
        lines.append(f"  - description: {it.get('description','')}")
        if it.get("evidence"):
            lines.append(f"  - evidence: {it['evidence']}")
    lines.append("")
    lines.append("### 隐式特征（implicit_traits）")
    imp = state.get("implicit_traits") or []
    if not imp:
        lines.append("（暂无 — 等待画像信号自动沉淀）")
    for it in imp:
        lines.append(f"- **{it.get('trait','未命名')}**")
        lines.append(f"  - description: {it.get('description','')}")
        if it.get("basis"):
            lines.append(f"  - basis: {it['basis']}")
        if it.get("evidence"):
            lines.append(f"  - evidence: {it['evidence']}")
    lines.append("")
    return "\n".join(lines)


# PROFILE.md 段标记（daemon 只替换此区间，保护文件其余部分）
SEC_START = "<!-- profile:auto-start -->"
SEC_END = "<!-- profile:auto-end -->"


def sync_profile_md(state):
    """Replace the auto-managed section of PROFILE.md with rendered state."""
    if not os.path.exists(PROFILE_MD):
        log("PROFILE.md missing; skip render")
        return
    _scrub_mojibake(state)
    with open(PROFILE_MD, "r", encoding="utf-8") as f:
        text = f.read()
    body = render_profile_md(state)
    section = f"\n{SEC_START}\n\n{body}\n{SEC_END}\n"
    if SEC_START in text and SEC_END in text:
        head, tail = text.split(SEC_START, 1)
        _, tail = tail.split(SEC_END, 1)
        text = head + section + tail
    else:
        text += "\n" + section
    tmp = PROFILE_MD + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, PROFILE_MD)


def state_to_prompt(state):
    """Render current profile as indexed JSON for UPDATE prompt."""
    parts = []
    exp = state.get("explicit_info") or []
    imp = state.get("implicit_traits") or []
    parts.append("=== explicit_info ===")
    for i, it in enumerate(exp):
        parts.append(f"[{i}] {json.dumps(it, ensure_ascii=False)}")
    parts.append("=== implicit_traits ===")
    for i, it in enumerate(imp):
        parts.append(f"[{i}] {json.dumps(it, ensure_ascii=False)}")
    return "\n".join(parts) if parts else "(empty profile)"


# ---------- EverOS 复刻 prompt（画像增量更新） ----------

PROFILE_UPDATE_PROMPT = """你是用户画像更新员。根据对话记录，判断需要对用户画像做哪些操作。

**目标用户：{target_user}**
只更新该用户本人的画像；其他参与者或 AI 助手的信息绝不要归入。

【当前用户画像】（每条有 index 编号）
{current_profile}

【对话记录】
{conversation}

【任务】输出操作列表（可多条），类型：add / update / delete / none。
- add    ：发现全新用户信息（与现有条目无关）
- update ：现有条目有信息补充/修改（用 index 指定）
- delete ：用户明确否定 / 信息已过时 / 与新信息直接矛盾
- none   ：对话不包含任何用户画像信息时

【重要规则】
1. explicit_info = 客观事实/当前状态（基本资料、健康、技能、明确偏好）；
   implicit_traits = 性格标签/决策风格（2-6字，如[风险厌恶型][数据考据党]）。
2. 去重：类似特征已存在（即使措辞不同）用 update 补充，不重复 add。
3. evidence 要含时间线索（如"2026年9月用户提到…"）。
4. 语言：使用与现有画像相同的语言（中文）输出。
5. 只输出 JSON，不要额外文字。

【输出格式】
无操作时：
{{"operations": [{{"action": "none"}}], "update_note": "无画像信息"}}
有操作时：
{{"operations": [
  {{"action": "add", "type": "explicit_info", "data": {{"category": "...", "description": "...", "evidence": "..."}}}},
  {{"action": "add", "type": "implicit_traits", "data": {{"trait": "...", "description": "...", "basis": "...", "evidence": "..."}}}},
  {{"action": "update", "type": "explicit_info", "index": 0, "data": {{"description": "..."}}}},
  {{"action": "delete", "type": "implicit_traits", "index": 1, "reason": "..."}}
], "update_note": "..."}}
"""

COMPACT_PROMPT = """当前用户画像共 {total} 条（explicit_info + implicit_traits），超过上限 {max_items}。
请精简至合计 ≤ {max_items} 条：合并同类项、提炼标签、删过时/短期状态。
保留每条字段完整（尤其 evidence）。语言用中文。

当前画像：
{profile_text}

直接输出 JSON：
{{"explicit_info": [{{"category":"...","description":"...","evidence":"..."}}],
  "implicit_traits": [{{"trait":"...","description":"...","basis":"...","evidence":"..."}}],
  "compact_note": "说明"}}
"""


# ---------- 模型调用（复用 judge 链路） ----------

def call_model(prompt, provider):
    # 不用 text=True：需要拿原始字节走显式解码（UTF-8 优先 -> GBK 兜底，损坏即失败），
    # 避免 2026-09-10 那类「GBK 字节按 UTF-8 errors=replace 读」的静默污染。
    r = subprocess.run(
        [sys.executable, ROUTER, "chat", "--provider", provider, prompt],
        capture_output=True, timeout=MODEL_TIMEOUT,
    )
    if r.returncode != 0:
        try:
            err = judge_mod.decode_child_output(r.stderr or b"")[:200]
        except Exception:
            err = f"({len(r.stderr or b'')}B undecodable stderr)"
        raise RuntimeError(f"router exit {r.returncode}: {err}")
    reply = judge_mod.decode_child_output(r.stdout or b"").strip()
    if not reply:
        raise RuntimeError("empty router reply")
    return reply


def parse_json_reply(reply):
    start = reply.find("{")
    end = reply.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object in reply")
    obj = json.loads(reply[start:end + 1])
    # 2026-09-10：拒绝错误载荷（router 失败时会把 {"error":...} 当文本吐回来）。
    # 旧实现直接 data.get("operations") or [] → 静默当作「无操作」，掩盖链路故障。
    if not isinstance(obj, dict) or "operations" not in obj:
        raise ValueError(f"reply has no 'operations' field (not a profile reply): {reply[:120]!r}")
    return obj


def read_err_state():
    try:
        with open(os.path.join(BASE, "scripts", ".profile_model_errors"), "r", encoding="utf-8") as f:
            cls, cnt = f.read().split("|")
            return cls, int(cnt)
    except Exception:
        return "", 0


def write_err_state(cls, cnt):
    try:
        with open(os.path.join(BASE, "scripts", ".profile_model_errors"), "w", encoding="utf-8") as f:
            f.write(f"{cls}|{cnt}")
    except Exception:
        pass


# ---------- 画像更新核心 ----------

def apply_ops(state, ops):
    exp = list(state.get("explicit_info") or [])
    imp = list(state.get("implicit_traits") or [])
    for op in ops:
        action = op.get("action")
        if action == "none":
            continue
        typ = op.get("type")
        target = exp if typ == "explicit_info" else imp
        data = op.get("data")
        if action == "add":
            if isinstance(data, dict) and data:
                target.append(data)
        elif action == "update":
            idx = op.get("index")
            if isinstance(idx, int) and 0 <= idx < len(target) and isinstance(data, dict):
                merged = dict(target[idx])
                merged.update(data)
                target[idx] = merged
        elif action == "delete":
            idx = op.get("index")
            if isinstance(idx, int) and 0 <= idx < len(target):
                target.pop(idx)
    state["explicit_info"] = exp
    state["implicit_traits"] = imp
    return state


def _need_compact(state):
    return (len(state.get("explicit_info") or []) +
            len(state.get("implicit_traits") or [])) > COMPACT_THRESHOLD


def compact_state(state, err_state):
    profile_text = json.dumps(
        {"explicit_info": state.get("explicit_info") or [],
         "implicit_traits": state.get("implicit_traits") or []},
        ensure_ascii=False, indent=2)
    prompt = COMPACT_PROMPT.format(total=len(state.get("explicit_info") or []) +
                                   len(state.get("implicit_traits") or []),
                                   max_items=MAX_ITEMS, profile_text=profile_text)
    reply = None
    last_err = ""
    for provider in MODEL_PROVIDERS:
        try:
            reply = call_model(prompt, provider)
            break
        except Exception as e:
            last_err = str(e)
    if reply is None:
        err_cls, cnt = err_state
        write_err_state("model_unavailable", cnt + 1)
        log(f"COMPACT_MODEL_FAIL #{cnt+1} (last: {last_err[:120]}) -> keep as-is")
        return state
    data = parse_json_reply(reply)
    state["explicit_info"] = data.get("explicit_info") or state.get("explicit_info") or []
    state["implicit_traits"] = data.get("implicit_traits") or state.get("implicit_traits") or []
    log(f"COMPACT done: {data.get('compact_note', '')[:120]}")
    return state


def update_profile(text, seq, state):
    """LLM incremental update. Returns update_note or None on hard failure."""
    err_cls, err_cnt = read_err_state()
    current = state_to_prompt(state)
    prompt = PROFILE_UPDATE_PROMPT.format(
        target_user="<USER>",
        current_profile=current,
        conversation=f"[最近对话片段] {text}",
    )
    reply = None
    last_err = ""
    for provider in MODEL_PROVIDERS:
        try:
            reply = call_model(prompt, provider)
            break
        except Exception as e:
            last_err = str(e)
    if reply is None:
        write_err_state("model_unavailable", err_cnt + 1)
        log(f"MODEL_FAIL #{err_cnt+1} (last: {last_err[:120]})")
        if err_cnt + 1 >= MAX_CONSEC_ERRORS:
            # 自锁写故障报告（对齐 distill/judge 三遍法则）
            try:
                with open(FAILURE_REPORT, "w", encoding="utf-8") as f:
                    f.write(f"# profile model unavailable (seq {seq})\n\n"
                            f"{datetime.datetime.now().isoformat(timespec='seconds')}\n"
                            f"连续 {err_cnt+1} 次模型失败；daemon 进入 BLOCKED 慢轮询。\n"
                            f"最近错误: {last_err[:300]}\n")
                open(BLOCKED_FILE, "w", encoding="utf-8").close()
            except Exception:
                pass
            log("BLOCKED: 3 consecutive model errors; slow-poll until recovery")
        return None
    # 模型成功：清错误计数
    if err_cnt:
        write_err_state("", 0)
    try:
        data = parse_json_reply(reply)
    except Exception as e:
        log(f"BAD_JSON from model: {str(e)[:100]} -> skip turn")
        return None
    ops = data.get("operations") or []
    if ops and ops[0].get("action") == "none":
        return data.get("update_note", "none")
    state = apply_ops(state, ops)
    if _need_compact(state):
        state = compact_state(state, read_err_state())
    state["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    save_state(state)
    sync_profile_md(state)
    return data.get("update_note", "")


# ---------- DB 轮询 ----------

def next_signal_turn(gt):
    """Oldest user turn (seq > gt) matching profile signal words."""
    conn = sqlite3.connect(HISTORY_DB, timeout=5)
    try:
        rows = conn.execute(
            "SELECT seq, content FROM conversation_history "
            "WHERE kind='context_msg' AND role='user' AND seq > ? "
            "ORDER BY seq ASC", (gt,)).fetchall()
    finally:
        conn.close()
    for seq, content in rows:
        text = (content or "").strip()
        if not text:
            continue
        if "\ufffd" in text:
            continue  # mojibake guard（对齐 distill/judge）
        if SIGNAL_RE.search(text) and len(text) >= 8:
            return seq, text
    return None


def run_cycle(cursor):
    n = 0
    while n < MAX_PER_CYCLE:
        turn = next_signal_turn(cursor)
        if turn is None:
            break
        seq, text = turn
        state = load_state()
        try:
            note = update_profile(text, seq, state)
            log(f"seq {seq}: {note or 'model-fail/skip'}")
        except Exception as e:
            log(f"seq {seq}: ERROR {str(e)[:150]}")
        finally:
            cursor = seq
            write_cursor(seq)
        n += 1
    return cursor


# ---------- 主循环 ----------

def try_probe_and_unblock():
    """BLOCKED 慢轮询中探活：一次极简模型调用成功 -> 解除。"""
    try:
        call_model("说一个字：好", MODEL_PROVIDERS[0])
    except Exception:
        return
    try:
        os.remove(BLOCKED_FILE)
    except OSError:
        pass
    write_err_state("", 0)
    log("RECOVERED: model probe ok; profile updates resumed")


def main():
    log("=== profile_daemon started ===")
    cursor = read_cursor()
    if cursor == 0:
        cursor = distill_db.get_max_seq()
        write_cursor(cursor)
        log(f"first run: cursor initialized to current max = {cursor}")
    while True:
        try:
            if os.path.exists(BLOCKED_FILE):
                try_probe_and_unblock()
                time.sleep(BLOCKED_POLL)
                continue
            cursor = run_cycle(cursor)
        except Exception as e:
            log(f"cycle ERROR: {str(e)[:200]}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()