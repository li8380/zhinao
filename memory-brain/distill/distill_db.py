#!/usr/bin/env python3
"""Distill conversation turns directly from QwenPaw history.db.

DB-direct engine (daemon edition). Same rules as distill.py:
- 3-layer filtering: skip kind=tool_result, skip short user turns, whitelist match
- Main category ranking: by keyword hit count, then priority
- Same-topic merge: process-discovery entries merge into following conclusion
- headline field used directly (no regex scraping from text)
"""
import sqlite3
import re
import datetime
import os

HISTORY_DB = r"C:\Users\<USER>\.qwenpaw\workspaces\default\history.db"

CATEGORIES = {
    "规则约束": ["只负责", "只管", "不该你", "越界", "归你管", "别动", "红线", "铁律",
              "只闲聊", "只指路", "先问", "准则", "纪律", "违规", "角色边界",
              "分区职责", "必须先", "不亲自动手", "只负责情报", "边界"],
    "决策": ["决定", "选择", "确定", "结论", "方案", "拍板", "策略", "定了"],
    "产出": ["创建", "生成", "完成", "安装", "配置", "接入", "删除", "实现", "写好", "做好", "跑通"],
    "偏好": ["喜欢", "不喜欢", "偏好", "习惯", "风格", "倾向", "爱用"],
    "待办": ["待办", "还没", "下次", "记得", "需要", "TODO", "todo", "待解决"],
    "纠正": ["错了", "修正", "应该是", "fix", "bug", "问题", "报错", "修复", "根因"],
    "工具": ["技能", "插件", "工具", "MCP", "API", "skill", "router", "open-websearch"],
    "发现": ["发现", "原来", "才知道", "了解到", "意识到", "根因", "排查"],
}

# Category priority when hit counts tie (higher = more important)
# 2026-09-08 先生拍板新增「规则约束」类（kevis 行为约束双写 AGENTS.md 常驻层），优先级最高
CATEGORY_PRIORITY = ["规则约束", "决策", "产出", "发现", "纠正", "工具", "待办", "偏好"]

# Minimum content length for user turns (skip short prompts like "继续完成")
MIN_USER_TURN_LEN = 15

# Max seq gap for merging process discoveries into a conclusion
MAX_MERGE_GAP = 60

# Max process notes shown per merged entry
MAX_PROCESS_NOTES = 2


def classify_scores(text):
    """Return ranked list of (category, hit_count) by hits desc then priority."""
    scores = {}
    for cat, keywords in CATEGORIES.items():
        hits = sum(1 for kw in keywords if kw in text)
        if hits > 0:
            scores[cat] = hits
    ranked = sorted(
        scores.items(),
        key=lambda item: (-item[1], CATEGORY_PRIORITY.index(item[0])),
    )
    return ranked


def extract_conclusion(text, headline):
    """Extract the most informative part of a turn."""
    # Priority 1: headline field (recall_history already summarized it)
    if headline and len(headline) > 5:
        return headline[:160]

    # Priority 2: explicit conclusion patterns
    patterns = [
        r'(?:结论|决定|方案|结果|已完成|拍板)[：:]\s*(.+?)(?:[。\n；;|]|$)',
        r'(?:锚点)[：:]\s*(.+?)(?:[。\n；;|]|$)',
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            conclusion = m.group(1).strip()
            if len(conclusion) > 160:
                conclusion = conclusion[:157] + "..."
            return conclusion if conclusion else text.strip()[:100]

    # Priority 3: pick the sentence with most keyword hits
    sentences = re.split(r'[。\n]', text)
    best = ""
    best_score = 0
    for s in sentences:
        s = s.strip()
        if len(s) < 10:
            continue
        score = sum(1 for cat_kws in CATEGORIES.values() for kw in cat_kws if kw in s)
        if score > best_score or (score == best_score and len(s) > len(best)):
            best = s
            best_score = score

    if best:
        if len(best) > 160:
            best = best[:157] + "..."
        return best

    # Last resort
    return text.strip()[:100].strip()


def fetch_turns(after_seq, before_seq=None):
    """Fetch candidate turns (skip tool_result) from history.db, seq ascending."""
    conn = sqlite3.connect(HISTORY_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    if before_seq is None:
        rows = cur.execute(
            "SELECT seq, kind, role, content, headline, created_at "
            "FROM conversation_history "
            "WHERE seq > ? AND kind != 'tool_result' "
            "ORDER BY seq ASC",
            (after_seq,),
        ).fetchall()
    else:
        rows = cur.execute(
            "SELECT seq, kind, role, content, headline, created_at "
            "FROM conversation_history "
            "WHERE seq > ? AND seq <= ? AND kind != 'tool_result' "
            "ORDER BY seq ASC",
            (after_seq, before_seq),
        ).fetchall()
    conn.close()
    return rows


def _has_mojibake(text):
    """True if text contains U+FFFD replacement char or undecodable bytes
    (mojibake from a mid-write sqlite snapshot caught by a concurrent daemon
    read — e.g. distill daemon reading a row QwenPaw is still writing). Such
    rows are transient garbage (source row is fine once flushed); skip them."""
    if not text:
        return False
    if "\ufffd" in text:
        return True
    # Some mojibake decodes to control/private chars instead of U+FFFD;
    # catch sequences that look undecodable by a quick utf-8 re-check.
    try:
        text.encode("utf-8")
        return False
    except UnicodeEncodeError:
        return True


def process_turn(row, entries):
    """Process one DB row: classify and extract if it matches whitelist."""
    seq = row["seq"]
    text = (row["content"] or "").strip()
    headline = row["headline"] or ""

    # 2026-09-04 guard: skip transient mojibake (concurrent mid-write snapshot)
    if _has_mojibake(text) or _has_mojibake(headline):
        return

    # Skip short user turns (prompts like "继续完成", "好的")
    if row["kind"] == "context_msg" and row["role"] == "user":
        if len(text) < MIN_USER_TURN_LEN:
            return

    # Whitelist match on content + headline
    ranked = classify_scores(text + "\n" + headline)
    if not ranked:
        return

    conclusion = extract_conclusion(text, headline)
    entries.append({
        "seq": seq,
        "categories": [cat for cat, _ in ranked[:2]],
        "conclusion": conclusion,
        "has_headline": bool(headline),
    })


def merge_entries(entries):
    """Merge process-discovery entries into their following conclusion entry.

    Rules:
    - An entry WITHOUT a headline is a "process discovery" (mid-debug finding).
    - If a conclusion entry (has headline) appears within MAX_MERGE_GAP seqs
      after the process entry, merge them: output as seq range, conclusion as
      main text, process notes as "(过程：...)" supplement.
    - Conclusion entries stand alone.
    """
    entries = sorted(entries, key=lambda e: e["seq"])
    merged = []
    i = 0
    n = len(entries)

    while i < n:
        e = entries[i]

        if e["has_headline"] or i == n - 1:
            # Conclusion entry, or last entry with no conclusion to merge into
            merged.append(e)
            i += 1
            continue

        # Process entry: try to find a conclusion within gap
        group = [e]
        j = i + 1
        while j < n and entries[j]["seq"] - e["seq"] <= MAX_MERGE_GAP:
            if entries[j]["has_headline"]:
                # Found conclusion: merge the whole group into it
                conclusion = entries[j]
                process_notes = [x["conclusion"] for x in group if x["conclusion"] != conclusion["conclusion"]]
                process_notes = process_notes[:MAX_PROCESS_NOTES]
                merged.append({
                    "seq": f"{e['seq']}-{conclusion['seq']}",
                    "categories": conclusion["categories"],
                    "conclusion": conclusion["conclusion"],
                    "process": process_notes,
                })
                i = j + 1
                break
            else:
                group.append(entries[j])
                j += 1
        else:
            # No conclusion found in gap: keep the process entry standalone
            merged.append(e)
            i += 1

    return merged


def distill_range(after_seq, before_seq=None):
    """Run full distill pipeline over seq range. Returns list of entry dicts."""
    rows = fetch_turns(after_seq, before_seq)
    entries = []
    for row in rows:
        process_turn(row, entries)
    return merge_entries(entries)


def entries_to_lines(entries):
    """Format entries to markdown lines."""
    lines = []
    for e in entries:
        cats = "/".join(e["categories"])
        line = f"- {e['seq']} | {cats} | {e['conclusion']}"
        if e.get("process"):
            line += "（过程：" + "→".join(e["process"]) + "）"
        lines.append(line)
    return lines


# --- 行级按 seq 去重（Memory Hub ②层：在线 judge 与 daemon 批量共用）---
SEQ_RE = re.compile(r"^-\s*(\d+)(?:-\d+)?\s*\|")
SINGLE_SEQ_RE = re.compile(r"^-\s*(\d+)\s*\|")


def line_seq_key(line):
    """Extract leading seq (single or range) from a diary line, or None."""
    m = SEQ_RE.match(line.strip())
    return m.group(1) if m else None


def dedup_lines_by_seq(existing_text, lines):
    """Dedup new lines by leading seq key (single e.g. '12345' or range
    '12345-12390'). First writer wins **per granularity**（2026-09-03 升级）：
    - 单行 vs 单行去重（judge 语义版/在线写）
    - 范围行 vs 范围行去重（distill 批量主题合并写）
    单行与范围行互不阻塞（不同粒度可共存：用户事实单行 + 主题合并范围行）。

    Returns (added_lines, skipped_count)."""
    single_keys = set()
    range_keys = set()
    for ln in existing_text.splitlines():
        k = line_seq_key(ln)
        if k is None:
            continue
        if SINGLE_SEQ_RE.match(ln.strip()):
            single_keys.add(k)
        else:
            range_keys.add(k)
    added = []
    skipped = 0
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        k = line_seq_key(ln)
        if k is None:
            continue
        if SINGLE_SEQ_RE.match(ln):
            if k in single_keys:
                skipped += 1
                continue
            single_keys.add(k)
        else:
            if k in range_keys:
                skipped += 1
                continue
            range_keys.add(k)
        added.append(ln)
    return added, skipped


def replace_lines_by_seq(existing_text, lines):
    """语义版优先（先生拍板 2026-09-03）：judge 模型语义判定 keep 时覆盖同 key 的旧单 seq 行。

    - 单 seq 行（`- 12345 | ...`）：语义版覆盖规则版；内容完全相同 -> skip（幂等）
    - 范围行（`- 12345-12390 | ...`）：不覆盖（保留 daemon 主题合并的内容），视为已占用 -> skip
    - 无旧行：正常追加

    Returns (final_text, added_lines, skipped_count, replaced_count).
    final_text 为重建后的完整文件内容（被替换的旧单行剔除 + 新行追加），调用方全量写回。"""
    range_keys = set()      # 范围行 key：不覆盖，仅去重
    single_rows = {}        # 单 seq 行 key -> content：可被覆盖
    for ln in existing_text.splitlines():
        k = line_seq_key(ln)
        if k is None:
            continue
        stripped = ln.strip()
        m = SINGLE_SEQ_RE.match(stripped)
        if m is not None and m.group(1) == k:
            single_rows[k] = stripped
        else:
            range_keys.add(k)
    added = []
    skipped = 0
    replaced = 0
    replaced_keys = set()
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        k = line_seq_key(ln)
        if k is None:
            continue  # 无法定位 seq 的行不写入（保持幂等性）
        if k in range_keys:
            skipped += 1
            continue
        if k in single_rows:
            if single_rows[k] == ln:
                skipped += 1  # 内容相同 -> 幂等跳过
            else:
                replaced += 1
                replaced_keys.add(k)
                single_rows[k] = ln
                added.append(ln)
            continue
        added.append(ln)
        single_rows[k] = ln
    # 重建完整文本：剔除被替换的旧单行，其余行保序保留，新行追加尾部
    final_lines = []
    for ln in existing_text.splitlines():
        k = line_seq_key(ln)
        if k in replaced_keys:
            continue
        final_lines.append(ln)
    final_lines.extend(added)
    final_text = "\n".join(final_lines)
    if final_text and not final_text.endswith("\n"):
        final_text += "\n"
    return final_text, added, skipped, replaced


def clean_mojibake_lines(memory_dir, today_only=False):
    """扫 memory/distill/*.md，删除含 U+FFFD 的**单 seq 行**。

    2026-09-10 先生拍板政策：**发现乱码直接清理，不浪费 tokens 去修复**——
    智脑是慢慢积攒的知识库，少一两条条目无所谓；重跑修复成本高、收益低。
    范围行与正常行永不触碰；手写笔记（memory/YYYY-MM-DD/*.md）不在范围内。

    Returns (removed_lines, files_touched).
    """
    if not os.path.isdir(memory_dir):
        return 0, 0
    removed = 0
    touched = 0
    names = sorted(os.listdir(memory_dir))
    if today_only:
        names = [f"{datetime.date.today().isoformat()}.md"]
    for name in names:
        if not name.endswith('.md') or '.bak' in name:
            continue
        path = os.path.join(memory_dir, name)
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.read().splitlines()
        except Exception:
            continue
        bad = set()
        for ln in lines:
            if '\ufffd' in ln and re.match(r'^-\s*\d+\s*\|', ln.strip()):
                bad.add(ln)
        if not bad:
            continue
        keep = [ln for ln in lines if ln not in bad]
        body = '\n'.join(keep)
        if body and not body.endswith('\n'):
            body += '\n'
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(body)
        except Exception:
            continue
        removed += len(bad)
        touched += 1
    return removed, touched


def get_max_seq():
    """Current max seq in history.db."""
    conn = sqlite3.connect(HISTORY_DB)
    cur = conn.cursor()
    v = cur.execute("SELECT MAX(seq) FROM conversation_history").fetchone()[0]
    conn.close()
    return v or 0


def get_last_seq_before(date_prefix):
    """Max seq with created_at LIKE 'YYYY-MM-DD%' (inclusive of that date)."""
    conn = sqlite3.connect(HISTORY_DB)
    cur = conn.cursor()
    v = cur.execute(
        "SELECT MAX(seq) FROM conversation_history WHERE created_at LIKE ?",
        (date_prefix + "%",),
    ).fetchone()[0]
    conn.close()
    return v or 0


def get_created_at(seq):
    """Return datetime (UTC aware) of a seq, or None."""
    conn = sqlite3.connect(HISTORY_DB)
    cur = conn.cursor()
    row = cur.execute(
        "SELECT created_at FROM conversation_history WHERE seq = ?", (seq,)
    ).fetchone()
    conn.close()
    if not row or not row[0]:
        return None
    try:
        return datetime.datetime.fromisoformat(row[0]).replace(tzinfo=datetime.timezone.utc)
    except Exception:
        return None


if __name__ == "__main__":
    import sys
    after = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    before = int(sys.argv[2]) if len(sys.argv) > 2 else None
    lines = entries_to_lines(distill_range(after, before))
    print(f"max_seq={get_max_seq()}")
    for ln in lines:
        print(ln)