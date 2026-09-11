#!/usr/bin/env python3
"""在线语义鉴定器 (Memory Hub ①层)

双层判定：
- 规则预筛（快路径，零成本）：tool_result / <6字 / 寒暄瞬时词表 / 短问句 → 不 keep
- 候选 → model-router 免费模型（agnes → modelscope）语义鉴定
    输出 {keep, entities, category, summary}
- 降级（三遍法则）：模型链路失败 → 规则兜底；同类错误连续 3 次 → 纯规则模式
    写故障报告段；纯规则模式定期探活，恢复后自动解除
- keep 条目 → memory/distill/YYYY-MM-DD.md（按 seq 去重，与 daemon 批量共用）

CLI:
  python judge.py --text "我有病，是焦虑症" [--seq 12345] [--kind user] [--dry-run]
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys

BASE = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
)
sys.path.insert(0, os.path.join(BASE, "skills", "memory-distillation", "scripts"))
import distill_db  # noqa: E402

ROUTER = os.path.join(BASE, "skills", "model-router", "router.py")
MEMORY_DIR = os.path.join(BASE, "memory", "distill")
STATE_DIR = os.path.join(BASE, "scripts")
LOG_FILE = os.path.join(STATE_DIR, "judge.log")
ERROR_STATE = os.path.join(STATE_DIR, ".judge_model_errors")
PURE_RULE_FLAG = os.path.join(STATE_DIR, ".judge_pure_rule")
FAILURE_REPORT = os.path.join(STATE_DIR, "distill_failure_report.md")

MODEL_PROVIDERS = ["gateway", "agnes", "modelscope"]  # 网关优先（软路由 simple 链，7×24+兜底），失败落 Agnes→ModelScope 直连双保险
MODEL_TIMEOUT = 25                             # 单次模型调用上限（s）
MAX_CONSEC_MODEL_ERRORS = 3                    # 三遍法则：同类错误连续 3 次 -> 降级
PROBE_INTERVAL_SEC = 600                       # 纯规则模式下探活间隔（s）

# 寒暄词表（v2：仅对短句 <15 字生效，防误杀长句中夹带的泛词；弱词走模型候选）
SMALLTALK = [
    "天气", "气温", "下雨", "晴天", "哈哈", "嗯", "好的", "收到", "谢谢",
    "再见", "晚安", "早上好", "吃了吗", "在吗", "咋样", "辛苦了",
    "加油", "随便", "干嘛", "告辞",
]
# 纯确认短句（≤10 字，全由确认词组成 -> 不 keep）
CONFIRM_ONLY = re.compile(r"^[好的可以行嗯收到没问题好吧okOK好继续谢谢了解知道了对]+$")
# 事实感词（含则短句也进候选，如"我有病，是焦虑症"）
FACT_HINTS = ["病", "痛", "症状", "诊断", "医生", "药", "血压", "血糖", "睡",
              "焦虑", "抑郁", "过敏", "喜欢", "讨厌", "习惯", "决定", "定了",
              "买了", "装了", "删了", "配置", "方案", "结论", "修", "换"]
# 规则约束感词（2026-09-08 维护者决定：助手行为约束信号，含则短句也进候选）
RULE_HINTS = ["只负责", "只管", "不该你", "越界", "归你管", "别动", "红线", "铁律",
              "只闲聊", "只指路", "先问", "准则", "纪律", "违规", "角色边界",
              "分区职责", "必须先", "不亲自动手", "边界"]


def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


# ---------- ① 规则预筛 ----------

def should_skip_prefilter(text, kind):
    """Return (skip, reason). True -> 不 keep（零成本快路径）。"""
    t = (text or "").strip()
    if not t:
        return True, "empty"
    # 系统注入块（context 压缩索引 / system-info 包装）不是用户陈述。
    # 2026-09-10 事故：judge 把 "<system-info>[context compressed]..." 判成
    # 用户事实，语义版又双写进 AGENTS.md 常驻层（已清理 + 此处拦截）。
    if t.startswith("<system-info>") or "[context compressed]" in t[:300]:
        return True, "system-injected"
    if kind == "tool_result":
        return True, "tool_result"
    if len(t) < 6:
        return True, "short<6"
    # 纯确认短句（≤10 字全确认词组成），如「好的」「可以」
    if len(t) <= 10 and CONFIRM_ONLY.match(t):
        return True, "confirm-short"
    # 强寒暄词：仅对短句（<15 字）生效，避免误杀长句（如「眼镜没事」夹带「没事」）
    if len(t) < 15:
        for w in SMALLTALK:
            if w in t:
                return True, f"smalltalk:{w}"
    # 短问句（<15 字且以问号/吗结尾，无事实感词/规则感词）→ 问题本身不存
    if len(t) < 15 and re.search(r"[？?吗]$", t) \
            and not any(f in t for f in FACT_HINTS + RULE_HINTS):
        return True, "short-question"
    return False, ""


# ---------- ② 模型鉴定 ----------

JUDGE_PROMPT = (
    "你是记忆鉴定器。判断以下用户陈述是否有值得长期留存的信息"
    "（关于用户的 事实 / 状态 / 决策 / 偏好 / 对助手的行为约束）。\n"
    "只输出一个 JSON 对象，不要输出任何其它文字：\n"
    '{{"keep": true或false, "entities": ["实体或关键词"], '
    '"category": "规则约束|偏好|决策|健康|状态|事实|待办|其他", "summary": "一句话提炼（≤40字）"}}\n'
    "判定规则：纯寒暄、瞬时变量（如天气）、无信息量 → keep=false；"
    "涉及用户自身状态/健康/偏好/决策/事实 → keep=true；"
    "涉及对助手的行为边界/角色约束/流程规则/分区职责"
    "（如“你只管X”“指挥室只闲聊指路”“以后先问再动”“这个归Y区”）"
    "→ keep=true 且 category=规则约束，summary 必须完整保留规则本体；"
    "信息不足无法提炼 → keep=false。\n"
    "陈述：{text}"
)


def decode_child_output(raw):
    """显式解码子进程输出：UTF-8 优先，GBK 兜底；解不干净就报错，绝不静默降级。

    2026-09-10 事故：router.py 的 stdout 编码取决于**继承的环境**——
    有 PYTHONIOENCODING=utf-8 时吐 UTF-8，被 schtasks 拉起时会退化为系统
    ANSI 代码页（cp936/GBK）。旧代码固定 encoding="utf-8" + errors="replace"，
    于是 GBK 中文被逐字节替换成 U+FFFD，**静默写进记忆库**（不可逆）。

    判定顺序（不可颠倒，否则会出现「锟斤拷」假中文）：
      1. 先按 UTF-8 严格解码
         - 成功且不含 U+FFFD -> 正常返回
         - 成功但含 U+FFFD -> 上游内容本身已损坏，直接失败

           （**不能**再拿 GBK 去解：U+FFFD 的 UTF-8 字节 0xEF 0xBF 0xBD
            恰好是合法 GBK 序列，会解出「锟斤拷」这种看似正常实则全错的
            文本，比直接报错危险得多）
      2. UTF-8 解码抛错 -> 说明是 GBK 字节流（cp936 stdout）-> 按 GBK 解
      3. 都失败 / 仍含替换符 -> 抛错，由调用方走三遍法则降级，不落盘
    """
    try:
        txt = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            txt = raw.decode("gbk")
        except UnicodeDecodeError:
            raise RuntimeError(
                f"undecodable router output ({len(raw)}B): neither utf-8 nor gbk "
                f"decodes cleanly -> refuse to persist mojibake"
            )
        if "\ufffd" in txt:
            raise RuntimeError(
                f"corrupted router output ({len(raw)}B): gbk decode contains "
                f"U+FFFD -> refuse to persist mojibake"
            )
        return txt

    if "\ufffd" in txt:
        raise RuntimeError(
            f"corrupted router output ({len(raw)}B): utf-8 decode contains "
            f"U+FFFD (upstream data already lossy) -> refuse to persist mojibake"
        )
    return txt


def call_model(text, provider):
    prompt = JUDGE_PROMPT.format(text=text)
    # 不用 text=True：需要拿原始字节自行解码（见 decode_child_output 说明）
    r = subprocess.run(
        [sys.executable, ROUTER, "chat", "--provider", provider, prompt],
        capture_output=True, timeout=MODEL_TIMEOUT,
    )
    if r.returncode != 0:
        try:
            err = decode_child_output(r.stderr or b"")[:200]
        except Exception:
            err = f"({len(r.stderr or b'')}B undecodable stderr)"
        raise RuntimeError(f"router exit {r.returncode}: {err}")
    reply = decode_child_output(r.stdout or b"").strip()
    if not reply:
        raise RuntimeError("empty router reply")
    return reply


def parse_json_reply(reply):
    """Extract JSON object from model reply. Raises on failure."""
    start = reply.find("{")
    end = reply.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no json in reply")
    obj = json.loads(reply[start:end + 1])
    # 2026-09-10 事故：模型链路失败时 router 会把错误载荷（如
    # {"error":{"message":"all channels in cooldown"}}）当文本吐回来。
    # 旧实现用 bool(obj.get("keep")) → 缺失字段静默变 False，
    # 于是「网关全挂」被记成「模型判定不该保留」，产出假判定。
    # 现在：没有 keep 字段一律报错，交由上游走降级/重试。
    if not isinstance(obj, dict) or "keep" not in obj:
        raise ValueError(f"reply has no 'keep' field (not a verdict): {reply[:120]!r}")
    keep = bool(obj.get("keep"))
    entities = [str(x) for x in (obj.get("entities") or [])][:5]
    category = str(obj.get("category") or "其他")
    summary = str(obj.get("summary") or "").strip()[:50]
    return keep, entities, category, summary


# ---------- ③ 规则兜底 ----------

def rule_fallback(text):
    """白名单兜底：命中关键词才 keep（降级/纯规则模式用）。"""
    ranked = distill_db.classify_scores(text)
    if ranked:
        return True, [], ranked[0][0], text.strip()[:40]
    return False, [], "其他", ""


# ---------- 三遍法则：降级状态 ----------

def read_error_state():
    """Return (error_class, count)."""
    if not os.path.exists(ERROR_STATE):
        return "", 0
    try:
        with open(ERROR_STATE, "r", encoding="utf-8") as f:
            parts = f.read().split("|")
            return parts[0], int(parts[1])
    except Exception:
        return "", 0


def write_error_state(cls, count):
    with open(ERROR_STATE, "w", encoding="utf-8") as f:
        f.write(f"{cls}|{count}")


def is_pure_rule():
    return os.path.exists(PURE_RULE_FLAG)


def enter_pure_rule():
    """同类错误连续 3 次 -> 降级纯规则模式 + 故障报告追加段。"""
    with open(PURE_RULE_FLAG, "w", encoding="utf-8") as f:
        f.write(datetime.datetime.now().isoformat(timespec="seconds") + "\n")
    try:
        with open(FAILURE_REPORT, "a", encoding="utf-8") as f:
            f.write(
                f"\n## judge 降级段（{datetime.datetime.now().isoformat(timespec='seconds')}）\n"
                f"- 语义鉴定模型连续 {MAX_CONSEC_MODEL_ERRORS} 次同类错误 -> 降级纯规则模式\n"
                f"- 处理建议：查看 `scripts/judge.log`；修好后删 `scripts\\.judge_pure_rule` 立即恢复，"
                f"或等 10 分钟自动探活解除\n"
            )
    except Exception:
        pass
    log("PURE_RULE: 3 consecutive model errors; degraded to rule-only mode")


def try_probe():
    """纯规则模式下探活：一次极简模型调用成功 -> 解除降级。"""
    try:
        call_model("说一个字：好", MODEL_PROVIDERS[0])
    except Exception:
        return False
    try:
        os.remove(PURE_RULE_FLAG)
    except OSError:
        pass
    write_error_state("", 0)
    log("JUDGE_RECOVERED: model probe ok; semantic judging resumed")
    return True


# ---------- ④ 留存 ----------

def write_entry(seq, categories, summary, overwrite=False):
    """Append entry line to today's distill diary.

    overwrite=False：按 seq 去重（先写者胜，daemon/规则兜底用）
    overwrite=True ：语义版优先（维护者决定）——模型语义判定 keep 时覆盖同 seq 旧单行
    Returns (added, skipped, replaced, path)."""
    if not categories or not summary:
        return 0, 0, 0, None
    # 落盘自检（2026-09-10 铁律第 5 条）：含替换符 U+FFFD 一律拒绝写入。
    # 宁可少一条，也绝不把乱码静默写进长期记忆（U+FFFD 不可逆）。
    if "\ufffd" in summary or "\ufffd" in str(categories):
        log(f"REFUSE_WRITE seq={seq}: mojibake (U+FFFD) in entry -> not persisted")
        return 0, 0, 0, None
    date_str = datetime.date.today().isoformat()
    path = os.path.join(MEMORY_DIR, f"{date_str}.md")
    os.makedirs(MEMORY_DIR, exist_ok=True)
    line = f"- {seq} | {categories} | {summary}"
    existing = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            existing = f.read()
    if overwrite:
        final_text, added, skipped, replaced = distill_db.replace_lines_by_seq(existing, [line])
        if added:
            with open(path, "w", encoding="utf-8") as f:
                f.write(final_text)
    else:
        added, skipped = distill_db.dedup_lines_by_seq(existing, [line])
        replaced = 0
        if added:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    return added, skipped, replaced, path


# ---------- ⑤ 规则约束类双写（2026-09-08 维护者决定） ----------

AGENTS_FILE = os.path.join(BASE, "AGENTS.md")
RULE_SECTION_TITLE = "## 🧠 智脑自动固化规则（judge 双写，常驻层）"
RULE_SECTION_HEADER = (
    RULE_SECTION_TITLE + "\n\n"
    "> judge_daemon 鉴定为「规则约束」类的条目自动双写到这里（常驻层每轮自动可见，"
    "消灭“档案室遗忘”）。按 seq 去重；同主题以最新条目为准。\n\n"
)


def write_rule_to_agents(seq, summary, agents_file=None, date_str=None):
    """规则约束类条目双写到 AGENTS.md 常驻层。幂等：按 seq 去重。

    Returns (added, path)."""
    path = agents_file or AGENTS_FILE
    date_str = date_str or datetime.date.today().isoformat()
    line = f"- [{date_str} seq={seq}] {summary}"
    # 常驻层（AGENTS.md）是每轮自动注入的，绝不能被乱码污染
    if "\ufffd" in line:
        log(f"REFUSE_RULE_WRITE seq={seq}: mojibake (U+FFFD) -> not persisted to {path}")
        return 0, path
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        content = ""
    # 幂等：同 seq 已存在 -> 跳过
    if f"seq={seq}]" in content:
        return 0, path
    if RULE_SECTION_TITLE not in content:
        if content and not content.endswith("\n"):
            content += "\n"
        content += "\n" + RULE_SECTION_HEADER + line + "\n"
    else:
        content += line + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    log(f"RULE_DUAL_WRITE seq={seq} -> {path}")
    return 1, path


def persist(seq, category, summary, overwrite=False, agents_file=None):
    """CLI 与 daemon 共用的落盘入口：写蒸馏 + 规则类双写 AGENTS.md 常驻层。

    Returns (added, skipped, replaced, path, rule_added)."""
    added, skipped, replaced, path = write_entry(seq, category, summary,
                                                 overwrite=overwrite)
    rule_added = 0
    if added and category == "规则约束":
        rule_added, _ = write_rule_to_agents(seq, summary, agents_file=agents_file)
    return added, skipped, replaced, path, rule_added


def judge_turn(text, kind="user"):
    """核心判定（CLI 与 judge_daemon 共用）：预筛 -> 模型/规则 -> 结果 dict。

    不做写入；keep=True 时调用方自行 write_entry。
    返回 dict：{keep, entities, category, summary, mode, reason?, prefilter?}"""
    text = (text or "").strip()

    # ① 预筛
    skip, reason = should_skip_prefilter(text, kind)
    if skip:
        return {"keep": False, "reason": reason, "prefilter": True}

    # 纯规则模式：先探活（间隔 PROBE_INTERVAL_SEC），失败走规则兜底
    if is_pure_rule():
        try:
            mtime = os.path.getmtime(PURE_RULE_FLAG)
            if (datetime.datetime.now().timestamp() - mtime) >= PROBE_INTERVAL_SEC:
                try_probe()
        except OSError:
            pass
        if is_pure_rule():
            keep, entities, cat, summary = rule_fallback(text)
            mode = "rule(pure)"
        else:
            keep, entities, cat, summary = rule_fallback(text)
            mode = "rule(probe-ok)"
        return {"keep": keep, "entities": entities, "category": cat,
                "summary": summary, "mode": mode}

    # ② 模型鉴定（候选链：agnes -> modelscope）
    err_cls, err_count = read_error_state()
    keep = None
    entities = []
    cat = "其他"
    summary = ""
    last_err = ""
    for provider in MODEL_PROVIDERS:
        try:
            reply = call_model(text, provider)
            keep, entities, cat, summary = parse_json_reply(reply)
            break
        except Exception as e:
            last_err = str(e)[:120]
            continue

    if keep is not None:
        # 模型成功：清计数
        if err_count:
            write_error_state("", 0)
        mode = "model"
    else:
        # ③ 模型链路失败 -> 规则兜底 + 三遍法则计数
        nxt = (err_count + 1) if err_cls == "model_unavailable" else 1
        write_error_state("model_unavailable", nxt)
        log(f"MODEL_FAIL #{nxt} (last: {last_err}) -> rule fallback")
        keep, entities, cat, summary = rule_fallback(text)
        mode = f"rule(model-fail#{nxt})"
        if nxt >= MAX_CONSEC_MODEL_ERRORS:
            enter_pure_rule()
            mode = "rule(pure-now)"

    return {"keep": keep, "entities": entities, "category": cat,
            "summary": summary, "mode": mode}


def main():
    ap = argparse.ArgumentParser(description="在线语义鉴定器 (Memory Hub ①)")
    ap.add_argument("--text", required=True, help="用户陈述文本")
    ap.add_argument("--seq", type=int, default=None, help="对应 seq（助手集成时传当前轮）")
    ap.add_argument("--kind", default="user", choices=["user", "tool_result", "context_msg"])
    ap.add_argument("--dry-run", action="store_true", help="只判定不写入")
    ap.add_argument("--memory-dir", default=None, help="蒸馏输出目录（默认 memory/distill；测试用）")
    ap.add_argument("--agents-file", default=None,
                    help="规则约束类双写目标（默认工作区 AGENTS.md；测试用临时文件）")
    args = ap.parse_args()

    global MEMORY_DIR
    if args.memory_dir:
        MEMORY_DIR = args.memory_dir

    text = (args.text or "").strip()
    seq = args.seq if args.seq is not None else distill_db.get_max_seq()

    result = judge_turn(text, args.kind)

    keep = result.get("keep")
    if not keep:
        print(json.dumps({"keep": False,
                          "reason": result.get("reason", "not-info"),
                          "mode": result.get("mode", "")}, ensure_ascii=False))
        return

    if args.dry_run:
        print(json.dumps({"keep": True, "entities": result["entities"],
                          "category": result["category"], "summary": result["summary"],
                          "mode": result["mode"], "dry_run": True}, ensure_ascii=False))
        return

    # 语义版优先（维护者决定）：仅模型判定（mode=model*）时覆盖；规则兜底保持先写者胜
    overwrite_ok = result["mode"].startswith("model")
    added, skipped, replaced, path, rule_added = persist(
        seq, result["category"], result["summary"],
        overwrite=overwrite_ok, agents_file=args.agents_file)
    print(json.dumps({"keep": True, "entities": result["entities"],
                      "category": result["category"], "summary": result["summary"],
                      "mode": result["mode"], "written": True,
                      "path": path, "added": added, "dup_skipped": skipped,
                      "replaced": replaced, "rule_dual_write": rule_added},
                     ensure_ascii=False))
    log(f"JUDGE keep seq={seq} cat={result['category']} summary={result['summary']} "
        f"mode={result['mode']} overwrite={overwrite_ok} rule_dual_write={rule_added}")


if __name__ == "__main__":
    main()