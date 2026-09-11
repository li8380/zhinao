#!/usr/bin/env python3
"""Distill conversation turns using whitelist keyword matching.

Usage: python3 distill.py <input_file> <output_file>

Reads raw turns text from recall_history, extracts whitelist-matching turns,
classifies them, and outputs formatted entries.

Features:
- 3-layer filtering: skip tool_result, skip short user turns, whitelist match
- Main category ranking: by keyword hit count, then priority
- Same-topic merge: process-discovery entries merge into their following
  conclusion entry (within MAX_MERGE_GAP seqs), output as seq range
"""
import sys
import re

CATEGORIES = {
    "决策": ["决定", "选择", "确定", "结论", "方案", "拍板", "策略", "定了"],
    "产出": ["创建", "生成", "完成", "安装", "配置", "接入", "删除", "实现", "写好", "做好", "跑通"],
    "偏好": ["喜欢", "不喜欢", "偏好", "习惯", "风格", "倾向", "爱用"],
    "待办": ["待办", "还没", "下次", "记得", "需要", "TODO", "todo", "待解决"],
    "纠正": ["错了", "修正", "应该是", "fix", "bug", "问题", "报错", "修复", "根因"],
    "工具": ["技能", "插件", "工具", "MCP", "API", "skill", "router", "open-websearch"],
    "发现": ["发现", "原来", "才知道", "了解到", "意识到", "根因", "排查"],
}

# Category priority when hit counts tie (higher = more important)
CATEGORY_PRIORITY = ["决策", "产出", "发现", "纠正", "工具", "待办", "偏好"]

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


def extract_headline(text):
    """Extract headline from recall_history format (headline=... created_at=...)."""
    # headline may contain '=', so match until ' created_at=<timestamp>'
    m = re.search(r'headline=(.+?)\s+created_at=\d{4}-\d{2}-\d{2}T', text)
    if m:
        return m.group(1).strip()
    return None


def extract_conclusion(text):
    """Extract the most informative part of a turn."""
    # Priority 1: headline field (recall_history already summarized it)
    headline = extract_headline(text)
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


def is_user_turn_short(text):
    """Check if a user turn is too short to contain real information."""
    # Remove metadata prefix (may have leading whitespace from split)
    content = re.sub(r'^\s*kind=context_msg role=user created_at=\S+\s*', '', text)
    content = content.strip()
    return len(content) < MIN_USER_TURN_LEN


def is_tool_result(text):
    """Check if a turn is a tool_result (command output) - skip these."""
    return bool(re.match(r'^\s*kind=tool_result\b', text))


def process_turn(seq, content_parts, entries):
    """Process a single turn: classify and extract if it matches whitelist."""
    if not content_parts:
        return
    turn_text = '\n'.join(content_parts)

    # Skip tool_result turns (command outputs, not real conversation)
    if is_tool_result(turn_text):
        return

    # Skip short user turns (prompts like "继续完成", "好的")
    if is_user_turn_short(turn_text):
        return

    ranked = classify_scores(turn_text)
    if ranked:
        conclusion = extract_conclusion(turn_text)
        entries.append({
            "seq": seq,
            "categories": [cat for cat, _ in ranked[:2]],
            "conclusion": conclusion,
            "has_headline": extract_headline(turn_text) is not None,
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


def main():
    if len(sys.argv) != 3:
        print("Usage: python3 distill.py <input_file> <output_file>", file=sys.stderr)
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = sys.argv[2]

    with open(input_file, 'r', encoding='utf-8') as f:
        content = f.read()

    # Split by "— seq=NNNN" markers (recall_history output format)
    # re.split with capturing group returns: [text, seq, text, seq, ...]
    parts = re.split(r'(?:—\s+seq=(\d+))', content)

    entries = []
    current_seq = None
    current_content = []

    for i, part in enumerate(parts):
        if i % 2 == 1:  # Odd indices are captured seq numbers
            # Process previous turn
            process_turn(current_seq, current_content, entries)
            current_seq = int(part)
            current_content = []
        else:  # Even indices are content
            current_content.append(part)

    # Process last turn
    process_turn(current_seq, current_content, entries)

    # Merge same-topic entries
    entries = merge_entries(entries)

    # Write output
    with open(output_file, 'w', encoding='utf-8') as f:
        for e in entries:
            cats = "/".join(e["categories"])
            line = f"- {e['seq']} | {cats} | {e['conclusion']}"
            if e.get("process"):
                line += "（过程：" + "→".join(e["process"]) + "）"
            f.write(line + "\n")


if __name__ == "__main__":
    main()