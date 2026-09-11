#!/usr/bin/env node
/**
 * epr_selftest.mjs — EPR 一键自检（回归测试）
 * 用法：node epr_selftest.mjs [--gateway http://<ROUTER_IP>:4100] [--timeout 180]
 *      环境变量 GWKEY 传网关 key
 *
 * 做什么：内置带已知锢点行号的样本 → 跑 epr_reduce.mjs → 断言验证器回溯是否可靠
 * 为何：2026-09-11 自检发现验证器短关键词假阴性 67%（匹配窗口下限硬编码 6）
 *      —— 修复后需一个常驻回归手段，改完随时复验。
 */
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
function arg(name, def) { const a = process.argv.slice(2); const i = a.indexOf('--' + name); return (i >= 0 && a[i + 1]) ? a[i + 1] : def; }
const GATEWAY = arg('gateway', 'http://<ROUTER_IP>:4100');
const TIMEOUT = arg('timeout', '180');
const REDUCER = path.join(__dirname, 'epr_reduce.mjs');
const SAMPLE = path.join(__dirname, 'epr_selftest_sample.auto.txt');
const OUTJSON = path.join(__dirname, 'epr_selftest_result.json');

// 内置样本：行号即真值（MARKER 在第 11 行）
const SAMPLE_TEXT = [
  'EPR 自检样本',
  '一、背景',
  '智脑模型网关部署在软路由，监听端口 4100。',
  '二、免费链路',
  'epr 路由依次为 openrouter 免费档、nvidia 免费档、modelscope 免费档。',
  '三、指标',
  '小样本实测压缩比达到二十一点九倍。',
  '四、保真',
  '证据收据必须回溯到原文真实行号，不得编造。',
  '五、标记',
  '本节含独特标记 MARKER-ALPHA-7731 用于回溯测试。',
  '六、运维',
  '看门狗每分钟探测一次服务存活状态。',
  '结束。',
  ''
].join('\n');
fs.writeFileSync(SAMPLE, SAMPLE_TEXT, 'utf8');

if (!fs.existsSync(REDUCER)) { console.error('[FATAL] epr_reduce.mjs not found next to selftest'); process.exit(1); }
if (!process.env.GWKEY)  { console.error('[WARN] GWKEY not set — gateway may reject'); }

console.error(`[selftest] running reducer (timeout ${TIMEOUT}s)...`);
const t0 = Date.now();
const r = spawnSync(process.execPath, [REDUCER, '--input', SAMPLE, '--gateway', GATEWAY, '--timeout', TIMEOUT], {
  encoding: 'utf8', env: process.env, timeout: (parseInt(TIMEOUT, 10) + 30) * 1000
});
const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
if (r.status !== 0) {
  console.error(`[FAIL] reducer exit=${r.status} after ${elapsed}s`);
  console.error(r.stderr || '');
  process.exit(1);
}

let j;
try { j = JSON.parse(r.stdout); } catch (e) { console.error('[FAIL] reducer stdout not JSON: ' + e.message); process.exit(1); }
fs.writeFileSync(OUTJSON, JSON.stringify(j, null, 2), 'utf8');

const receipts = j.receipts || [];
const verified = receipts.filter(x => !x.unverified);
const rate = receipts.length ? verified.length / receipts.length : 0;
const marker = receipts.find(x => (x.key || '').includes('MARKER-ALPHA-7731'));

const checks = [
  ['收据数 >= 5', receipts.length >= 5, `receipts=${receipts.length}`],
  ['回溯命中率 >= 60%', rate >= 0.6, `${(rate * 100).toFixed(0)}% (${verified.length}/${receipts.length})`],
  ['MARKER 锚点回溯到 L11', !!marker && marker.line === 11, marker ? `line=${marker.line} declared=${marker.declared_line}` : 'not found'],
  ['输出含压缩比', typeof j.ratio === 'string' && j.ratio !== '0', `ratio=${j.ratio}`]
];

console.log('=== EPR SELFTEST ===');
console.log(`elapsed: ${elapsed}s  chunks: ${j.chunks}  in:${j.in_chars}chars  out:${j.out_chars}chars  ratio:${j.ratio}`);
let ok = true;
for (const [name, pass, detail] of checks) {
  console.log(`${pass ? 'PASS' : 'FAIL'}  ${name}  [${detail}]`);
  if (!pass) ok = false;
}
console.log(ok ? 'RESULT: ALL PASS' : 'RESULT: FAILED');
process.exit(ok ? 0 : 1);
