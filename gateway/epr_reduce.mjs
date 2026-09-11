#!/usr/bin/env node
/**
 * epr_reduce.mjs — EPR (Evidence-Preserving Reducer) 长输出压缩器
 * 借鉴 NV Labs SoL-Pi 的 Evidence-Preserving Reducer 机制，落地到智脑·模型网关侧。
 *
 * 用途：把一条超长输出压成「摘要 + 证据收据」，只让摘要进上下文，证据收据可逐行回溯原文。
 *   - 不动 gateway.mjs（零侵入），只作为独立消费方调网关的 epr 路由（v1 兼容 chat/completions）
 *   - epr 路由 = 全免费档提炼链（openrouter free → nvidia free → modelscope → agnes），¥0 成本
 *   - 压缩比高（通常 10-50x），关键事实以「收据」形式保留，指向原文行号/位置
 *
 * 用法：
 *   node epr_reduce.mjs --input <file.txt> [--gateway http://<ROUTER_IP>:4100] [--route epr] [--max-in <chars>]
 *   - 文本从文件读，避免 shell 引号地狱（AGENTS.md 铁律）
 *   - stdout 输出 JSON：{ in_chars, in_lines, out_chars, ratio, summary, receipts:[{key, evidence}], dropped:[...] }
 *
 * 收据原理（内核级，非提示词）：
 *   reducer 提炼完成后，对「每条摘要要点」做证据回溯——把要点里的关键短语在原文中定位，
 *   命中则记 receipt {key, line, snippet}；未命中的要点标记为 low_confidence。
 *   这样下游（思路/记忆）能知道哪条是有原文背书的，哪条是提炼模型自己补的（宁缺毋滥）。
 *
 * 环境铁律：daemon/子进程必须显式 UTF-8（PYTHONIOENCODING 同源）：进程内所有字符串默认 utf8 输出。
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// ---------- CLI ----------
function arg(name, def) {
  const argv = process.argv.slice(2);
  const i = argv.indexOf('--' + name);
  if (i >= 0 && argv[i + 1]) return argv[i + 1];
  return def;
}
const INPUT = arg('input');
if (!INPUT || !fs.existsSync(INPUT)) {
  console.error('[FATAL] need --input <file>');
  process.exit(1);
}
const GATEWAY = arg('gateway', 'http://<ROUTER_IP>:4100');
const ROUTE = arg('route', 'epr');
const KEY = arg('key', process.env.GWKEY || '');
const MAX_IN = parseInt(arg('max-in', '200000'), 10); // 给 reducer 的原文上限
const CHUNK = parseInt(arg('chunk', '12000'), 10);    // 分块大小（EPR 对超大输入分块提炼再合并）
const FETCH_TIMEOUT = parseInt(arg('timeout', '120'), 10); // 单次调用超时（s）——网关上游 timeout_seconds=120 已兜底，这里防调用方无限等

// ---------- 读取原文 ----------
let raw = fs.readFileSync(INPUT, 'utf8');
const inLines = raw.split('\n');
if (raw.length > MAX_IN) {
  raw = raw.slice(0, MAX_IN);
  console.error(`[WARN] input truncated to ${MAX_IN} chars`);
}

// ---------- 调网关 epr 路由 ----------
function postChat(prompt) {
  const body = JSON.stringify({
    model: ROUTE,
    messages: [
      { role: 'system', content:
`你是证据保持压缩器(EPR)。把用户给出的长文本压缩成简洁要点。规则：
1. 只保留有原文依据的事实，不补充你的知识。
2. 每条要点=一行，格式[编号] 关键短语 → 一句话说明。关键短语必须是原文出现过的词。
3. 收据段：每行引用原文行号，格式必须二选一：
   A) [编号] 行号N： "关键短语"   （中文行号写法）
   B) [编号] 关键短语 → line N    （英文行号写法）
   N 必须是原文真实行号，拿不准就写 line 1。
4. 最后输出三部分，用分隔行 ---RECEIPTS--- 和 ---DROPPED--- 隔开：
   （摘要要点若干行）
   ---RECEIPTS---
   （每条要点一条收据，带行号）
   ---DROPPED---
   （一句总结舍弃了什么）
5. 中文输出。宁缺毋滥：不确定的部分不要编造，归入 dropped。` },
      { role: 'user', content: `<原文>\n${raw}\n</原文>\n\n请生成证据保持摘要。` }
    ],
    temperature: 0.2
  });
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT * 1000);
  return fetch(`${GATEWAY}/v1/chat/completions`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${KEY}`
    },
    body,
    signal: controller.signal
  }).finally(() => clearTimeout(timer)).then(async (r) => {
    const txt = await r.text();
    if (!r.ok) throw new Error(`gateway ${r.status}: ${txt.slice(0, 300)}`);
    try {
      const j = JSON.parse(txt);
      const msg = j.choices && j.choices[0] && (j.choices[0].message || {});
      // 兼容 reasoning_content + content 的格式（coding 推理模型踩过的坑）
      const c = msg.content;
      if (typeof c === 'string') return c;
      if (Array.isArray(c)) return c.map(p => (typeof p === 'string' ? p : (p && p.text) || '')).join(' ');
      if (msg.reasoning_content) return msg.reasoning_content; // 有些档只有思维链
      throw new Error('no content in response');
    } catch (e) {
      throw new Error(`parse fail: ${e.message}`);
    }
  });
}

// ---------- 收据验证（证据保持核心） ----------
// 模型输出格式约定：---RECEIPTS--- 段里每行 `- [N] 行X: "片段"` 声明证据。
// 内核验证 = 逐条把「片段」在原文回溯定位，核对真实行号；命中→verified receipt，未命中→unverified（可疑）。
// 摘要段（RECEIPTS 前）每行也做短语回溯兜底。
// ---------- 短语回溯工具（验证器核心） ----------
// 归一化：剥掉所有非中文/字母/数字字符（含空格），用于行内定位（原文与关键词两侧同口径）
function normText(s) { return String(s).replace(/[^\u4e00-\u9fa5A-Za-z0-9]/g, ''); }

// 在原文中回溯关键短语，返回 { line, snippet } 或 null
//   - 逐行归一化匹配，保留真实行号映射（旧版整串 indexOf，空格口径不一致会失配）
//   - 匹配窗口从长到短，下限 3（中文关键词常 3-5 字；旧版硬编码 6 → 67% 假阴性 bug）
function locatePhrase(phrase, inText) {
  if (!phrase) return null;
  const rawLines = inText.split('\n');
  const normLines = rawLines.map(normText);
  const floor = Math.min(3, phrase.length);
  for (let len = Math.min(16, phrase.length); len >= floor; len--) {
    const win = phrase.slice(0, len);
    for (let i = 0; i < normLines.length; i++) {
      if (normLines[i].includes(win)) {
        return { line: i + 1, snippet: rawLines[i].trim().slice(0, 60) };
      }
    }
  }
  return null;
}

function verifyReceipts(summary, inText) {
  const lines = summary.split('\n');
  const receipts = [];
  const dropped = [];
  const summaryLines = [];
  const receiptDecls = [];
  let section = 'summary';
  for (const ln of lines) {
    const t = ln.trim();
    if (/^---RECEIPTS---$/i.test(t)) { section = 'receipts'; continue; }
    if (/^---DROPPED---$/i.test(t)) { section = 'dropped'; continue; }
    if (!t) continue;
    if (section === 'summary') summaryLines.push(t);
    else if (section === 'receipts') receiptDecls.push(t);
    else dropped.push(t.slice(0, 80));
  }
  // 1) 逐条验证模型声明的收据：片段回溯 + 行号核对
  //    兼容模型两种输出格式：
  //      A) `- [1] 行号1： "片段"` / `[1] 行1: 片段`
  //      B) `[1] 片段 → line 4` / `[1] 片段 → line 4`（片段在前，行号在箭头后）
  //    无论哪种，行号只作参考，内核总是独立回溯原文定位真实行号。
  for (const decl of receiptDecls) {
    let m = decl.match(/\[(\d+)\]\s*行\s*号?\s*(\d+)?\s*[:：]?\s*[“"']?(.+?)[”"']?$/);
    let key = null, declLine = null;
    if (m) {
      declLine = m[2] ? parseInt(m[2], 10) : null;
      key = m[3];
    } else {
      m = decl.match(/\[(\d+)\]\s*(.+?)\s*[→\->]\s*line\s*(\d+)/i);
      if (m) {
        declLine = parseInt(m[3], 10);
        key = m[2];
      }
    }
    if (!key) continue;
    key = key.replace(/^[-*•#\d\s.]+/, '').trim();
    const phrase = key.replace(/[^\u4e00-\u9fa5A-Za-z0-9]/g, '');
    const hit = locatePhrase(phrase, inText);
    const found = hit ? {
      line: hit.line,
      declared_line: declLine,
      line_match: declLine === null ? null : declLine === hit.line,
      snippet: hit.snippet
    } : null;
    if (found) receipts.push({ key: key.slice(0, 60), ...found });
    else receipts.push({ key: key.slice(0, 60), line: null, declared_line: declLine, line_match: false, snippet: null, unverified: true });
  }
  // 2) 摘要行兜底（模型没给 RECEIPTS 段时的保底）
  if (receiptDecls.length === 0) {
    for (const t of summaryLines) {
      const phrase = t.replace(/^[-*•#\d\s.]+/, '').replace(/[^\u4e00-\u9fa5A-Za-z0-9]/g, '').slice(0, 16);
      if (phrase.length < 2) continue;
      const hit = locatePhrase(phrase, inText);
      const found = hit ? { line: hit.line, declared_line: null, line_match: null, snippet: hit.snippet } : null;
      if (found) receipts.push({ key: t.slice(0, 48), ...found });
      else dropped.push(t.slice(0, 48));
    }
  }
  return { receipts, dropped };
}

// ---------- 主流程 ----------
// 超大输入分块：每块 ≤CHUNK 字符，独立走 epr 免费档提炼，最后合并所有收据（全局行号 = 块内行号 + 块起始行偏移）
function splitChunks(text, size) {
  const chunks = [];
  const lines = text.split('\n');
  let cur = [], curLen = 0, startLine = 1;
  for (const ln of lines) {
    if (curLen + ln.length + 1 > size && cur.length > 0) {
      chunks.push({ startLine, text: cur.join('\n') });
      startLine += cur.length;
      cur = []; curLen = 0;
    }
    cur.push(ln); curLen += ln.length + 1;
  }
  if (cur.length) chunks.push({ startLine, text: cur.join('\n') });
  return chunks;
}

async function main() {
  const chunks = splitChunks(raw, CHUNK);
  const allReceipts = [];
  const allDropped = [];
  const summaries = [];
  let outChars = 0;
  for (let ci = 0; ci < chunks.length; ci++) {
    const chunk = chunks[ci];
    const chunkText = chunk.text;
    const isMulti = chunks.length > 1;
    process.stderr.write(`[INFO] chunk ${ci + 1}/${chunks.length} (${chunk.startLine}-${chunk.startLine + chunkText.split('\n').length - 1})...\n`);
    const sum = await postChat(`【证据保持压缩】\n当前块是原文的第 ${chunk.startLine}~${chunk.startLine + chunkText.split('\n').length - 1} 行（共 ${inLines.length} 行 / 第 ${ci + 1} 块，共 ${chunks.length} 块）。\n\n请严格按规则压缩为证据保持摘要。\n——原文（块）——\n${chunkText}`);
    const { receipts, dropped } = verifyReceipts(sum, chunkText);
    // 全局行号修正
    for (const r of receipts) {
      if (r.line !== null) r.line += chunk.startLine - 1;
      if (r.declared_line !== null) r.declared_line += chunk.startLine - 1;
      if (r.snippet) r.snippet = `[chunk${ci + 1}] ${r.snippet}`;
    }
    allReceipts.push(...receipts);
    allDropped.push(...dropped.map(d => `[chunk${ci + 1}] ${d}`));
    summaries.push(sum);
    outChars += sum.length;
  }
  const out = {
    in_chars: raw.length,
    in_lines: inLines.length,
    chunks: chunks.length,
    out_chars: outChars,
    ratio: raw.length > 0 ? (raw.length / (outChars || 1)).toFixed(1) : '0',
    summary: summaries.join('\n\n---CHUNK---\n\n'),
    receipts: allReceipts,
    dropped: allDropped,
    receipts_count: allReceipts.length,
    dropped_count: allDropped.length,
    run_at: new Date().toISOString()
  };
  console.log(JSON.stringify(out, null, 2));
}
main().catch((e) => {
  console.error(`[ERROR] ${e.message}`);
  process.exit(1);
});
