#!/usr/bin/env node
/**
 * auto_route.mjs — 模型网关自动降级器（③决策反哺，2026-09-10）
 *
 * 扫网关日志最近窗口内各渠道的失败/冷却事件（switch/cooldown），
 * 事件数 >= 阈值 的渠道 = 「不稳定」，生成 route_overrides.json：
 *   - 只降级不升级：把不稳定渠道移到所有路由链的链尾（keep 在前、moved 在后）
 *   - 事件清零后写空 routes → 网关自动恢复 config.json 默认顺序
 * 网关 gateway.mjs 每 60s 热加载 route_overrides.json，写文件即生效。
 *
 * 用法：
 *   node auto_route.mjs [logfile] [outfile] [threshold]
 *     logfile   默认 /root/watch/log/gateway_svc.log
 *     outfile   默认 /root/model-gateway/route_overrides.json
 *     threshold 默认 3（窗口内失败/冷却事件数）
 * crontab: every 5 minutes  ->  node /root/model-gateway/auto_route.mjs
 */
import fs from 'node:fs';

const LOG = process.argv[2] || '/root/watch/log/gateway_svc.log';
const OUT = process.argv[3] || '/root/model-gateway/route_overrides.json';
const THRESHOLD = parseInt(process.argv[4] || '3', 10);
const CFG = '/root/model-gateway/config.json';
const WINDOW_MS = 15 * 60 * 1000;

// ---------- 读配置（路由链结构） ----------
let config;
try {
  config = JSON.parse(fs.readFileSync(CFG, 'utf8'));
} catch (e) {
  console.error(`auto_route: config read fail: ${e.message}`);
  process.exit(1);
}

// ---------- 扫日志：统计窗口内每渠道事件数 ----------
const events = {}; // channelId -> count
try {
  const now = Date.now();
  const raw = fs.readFileSync(LOG, 'utf8');
  for (const line of raw.split('\n')) {
    const m = line.match(/^(\S+) \[(WARN|INFO)\] (.*)$/);
    if (!m) continue;
    const ts = new Date(m[1]).getTime();
    if (!ts || now - ts > WINDOW_MS) continue;
    const msg = m[3];
    // 匹配两种日志：`channel X in cooldown ...` / `switch X -> next ...`
    const idm = msg.match(/channel (\S+) in cooldown/) || msg.match(/switch (\S+) ->/);
    if (!idm) continue;
    const id = idm[1];
    events[id] = (events[id] || 0) + 1;
  }
} catch (e) {
  console.error(`auto_route: log read fail (${LOG}): ${e.message}`);
  // 日志缺失时不清除现有 overrides（保持上次决策），退出
  process.exit(1);
}

// ---------- 生成 overrides ----------
const demote = Object.entries(events)
  .filter(([, n]) => n >= THRESHOLD)
  .map(([id]) => id);

const routes = {};
if (demote.length) {
  for (const [rname, chain] of Object.entries(config.routes || {})) {
    if (!Array.isArray(chain)) { routes[rname] = chain; continue; }
    const keep = chain.filter((x) => !demote.includes(x.id));
    const moved = chain.filter((x) => demote.includes(x.id));
    if (moved.length) routes[rname] = [...keep, ...moved];
  }
}

const out = {
  updated_at: new Date().toISOString(),
  demoted: demote,
  routes,
  note: demote.length
    ? `auto: demote ${demote.map((id) => `${id}(x${events[id]})`).join(', ')}`
    : 'auto: stable, no demotion',
};

// 无论是否降级都写（空 routes 也能清除旧的降级，自动恢复）
try {
  fs.writeFileSync(OUT, JSON.stringify(out, null, 2) + '\n');
  console.log(`auto_route: ${out.note} (routes=${Object.keys(routes).join(',') || 'none'})`);
} catch (e) {
  console.error(`auto_route: write fail (${OUT}): ${e.message}`);
  process.exit(1);
}
