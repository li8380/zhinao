#!/usr/bin/env node
/**
 * gateway.mjs — OpenAI 兼容模型网关 v1.1（Node 零依赖）
 * v1.1(2026-09-10): 新增 orchestrator 智能分流
 *   - model=orchestrator → 按最后一条 user 消息关键词分流：
 *       coding(编程/报错/脚本/SQL/数据分析) > brain(方案/总结/报告) > simple(简短问候) > 默认 brain
 *   - 原有 main/chat/cheap 及任意固定链路由不变
 *   - 新增 brain/coding/simple 三个链 + 各自兜底
 * v1.0(2026-09-09): 聚合多路渠道，按失败类型自动切换（网络/429/5xx 切；401/400 不切）；冷却防击打
 */

import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// ---------- 配置 ----------
function loadConfig() {
  const argv = process.argv.slice(2);
  const idx = argv.indexOf('--config');
  const cfgPath = idx >= 0 && argv[idx + 1] ? argv[idx + 1] : path.join(__dirname, 'config.json');
  if (!fs.existsSync(cfgPath)) {
    console.error(`[FATAL] config not found: ${cfgPath}`);
    process.exit(1);
  }
  const cfg = JSON.parse(fs.readFileSync(cfgPath, 'utf8'));
  if (Array.isArray(cfg.channels) === false || cfg.channels.length === 0) {
    console.error('[FATAL] config.channels must be a non-empty array');
    process.exit(1);
  }
  return cfg;
}

const config = loadConfig();
const PORT = config.port || 4100;
const COOLDOWN_MS = (config.cooldown_seconds || 300) * 1000;
const TIMEOUT_MS = (config.timeout_seconds || 120) * 1000;
const LOG_FILE = config.log_file ? path.resolve(config.log_file) : null;

// ---------- 日志 ----------
function log(level, msg) {
  const line = `${new Date().toISOString()} [${level}] ${msg}`;
  if (LOG_FILE) {
    try { fs.appendFileSync(LOG_FILE, line + '\n'); } catch (_) { console.log(line); }
  } else {
    console.log(line);
  }
}

// ---------- 渠道状态：冷却 ----------
const cooldownUntil = new Map(); // channelId -> timestamp

// ---------- v1.2: 健康统计（②感知 /stats 用） ----------
const stats = {
  startedAt: Date.now(),
  totalRequests: 0,
  byRoute: {},    // routeName -> { requests, ok, switched, failed }
  byChannel: {},  // channelId -> { requests, ok, switched, failed, cooldowns }
};
function bump(obj, key, field) {
  obj[key] = obj[key] || { requests: 0, ok: 0, switched: 0, failed: 0, cooldowns: 0 };
  obj[key][field] = (obj[key][field] || 0) + 1;
}

// ---------- v1.2: route_overrides.json 热加载（③反哺，60s 轮询 mtime） ----------
const OVERRIDES_FILE = path.join(__dirname, 'route_overrides.json');
let overrides = { routes: {}, note: '', updated_at: '' };
let overridesMtime = 0;

function loadOverrides(force) {
  try {
    const st = fs.statSync(OVERRIDES_FILE);
    if (!force && st.mtimeMs === overridesMtime) return;
    const data = JSON.parse(fs.readFileSync(OVERRIDES_FILE, 'utf8'));
    if (data && data.routes && typeof data.routes === 'object') {
      overrides = data;
      overridesMtime = st.mtimeMs;
      log('INFO', `overrides loaded: routes=${Object.keys(data.routes).join(',')} note=${data.note || ''}`);
    }
  } catch (e) {
    if (e.code === 'ENOENT') {
      // 文件被删 → 重置为默认路由（运行中恢复）
      if (overridesMtime !== 0 || Object.keys(overrides.routes || {}).length) {
        overrides = { routes: {}, note: '', updated_at: '' };
        overridesMtime = 0;
        log('INFO', 'overrides removed: reset to default config routes');
      }
    } else {
      log('WARN', `overrides read fail: ${e.message}`);
    }
  }
}

function effectiveRoutes() {
  const r = {};
  for (const [k, v] of Object.entries(config.routes || {})) r[k] = v;
  for (const [k, v] of Object.entries(overrides.routes || {})) {
    if (Array.isArray(v)) r[k] = v;
  }
  return r;
}

loadOverrides(true);
setInterval(() => loadOverrides(false), 60000);

function isInCooldown(ch) {
  const t = cooldownUntil.get(ch.id);
  return t !== undefined && t > Date.now();
}

function markCooldown(ch, reason) {
  cooldownUntil.set(ch.id, Date.now() + COOLDOWN_MS);
  log('WARN', `channel ${ch.id} in cooldown ${Math.round(COOLDOWN_MS / 1000)}s: ${reason}`);
}

// ---------- 判定失败类型 ----------
function classify(status, isNetworkError) {
  if (isNetworkError) return 'switch';
  if (status === 429 || (status >= 500 && status <= 599)) return 'switch';
  if (status === 401) return 'fatal';
  if (status >= 400 && status < 500) return 'fatal';
  return 'ok';
}

function chatEndpoint(baseUrl) {
  const b = baseUrl.endsWith('/') ? baseUrl.slice(0, -1) : baseUrl;
  return b + '/chat/completions';
}

// ---------- 单渠道转发 ----------
async function callChannel(ch, body, model, signal) {
  const headers = {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${ch.api_key}`,
    ...(ch.custom_headers || {}),
  };
  const payload = { ...body, model };
  const resp = await fetch(chatEndpoint(ch.base_url), {
    method: 'POST',
    headers,
    body: JSON.stringify(payload),
    signal,
  });
  return resp;
}

// ---------- 渠道链尝试 ----------
async function tryChain(routeName, route, body) {
  stats.totalRequests++;
  bump(stats.byRoute, routeName, 'requests');

  const active = route.filter(({ id }) => {
    const ch = config.channels.find(c => c.id === id);
    return ch && !isInCooldown(ch);
  });

  if (active.length === 0) {
    bump(stats.byRoute, routeName, 'failed');
    return { status: 503, json: { error: { message: 'all channels in cooldown', type: 'gateway_cooldown' } } };
  }

  const responses = [];

  for (const { id, model } of active) {
    const ch = config.channels.find(c => c.id === id);
    bump(stats.byChannel, id, 'requests');
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
    try {
      const resp = await callChannel(ch, body, model, controller.signal);
      const cls = classify(resp.status, false);

      if (cls === 'ok') {
        bump(stats.byRoute, routeName, 'ok');
        bump(stats.byChannel, id, 'ok');
        if (body.stream) {
          log('INFO', `route '${routeName}' -> ${id} (${model}) streaming`);
          return { status: resp.status, stream: resp.body, extraHeaders: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' } };
        }
        const text = await resp.text();
        log('INFO', `route '${routeName}' -> ${id} (${model}) ok`);
        return { status: resp.status, text, extraHeaders: { 'Content-Type': resp.headers.get('content-type') || 'application/json' } };
      }

      if (cls === 'switch') {
        bump(stats.byRoute, routeName, 'switched');
        bump(stats.byChannel, id, 'switched');
        bump(stats.byChannel, id, 'cooldowns');
        const txt = await resp.text().catch(() => '');
        const reason = `${id} HTTP ${resp.status}: ${txt.slice(0, 200)}`;
        responses.push(reason);
        markCooldown(ch, `HTTP ${resp.status}`);
        log('INFO', `route '${routeName}' switch ${id} -> next (${reason})`);
        continue;
      }

      bump(stats.byRoute, routeName, 'failed');
      bump(stats.byChannel, id, 'failed');
      const txt = await resp.text().catch(() => '');
      return { status: resp.status, text: txt, extraHeaders: { 'Content-Type': resp.headers.get('content-type') || 'application/json' } };

    } catch (err) {
      bump(stats.byRoute, routeName, 'switched');
      bump(stats.byChannel, id, 'switched');
      bump(stats.byChannel, id, 'cooldowns');
      const reason = err.name === 'AbortError' ? `timeout > ${Math.round(TIMEOUT_MS / 1000)}s` : `network: ${err.message}`;
      responses.push(`${id} ${reason}`);
      markCooldown(ch, reason);
      log('INFO', `route '${routeName}' switch ${id} -> next (${reason})`);
      continue;
    } finally {
      clearTimeout(timer);
    }
  }

  bump(stats.byRoute, routeName, 'failed');
  return {
    status: 503,
    json: { error: { message: `all channels failed for model '${routeName}'`, type: 'gateway_failover', details: responses } },
  };
}

// ================= v1.1: orchestrator 分流 =================
// 关键词表（按优先级：coding 最高，brain 次之，simple 兜底问候）
// 实际规则：命中 coding 词 → coding 链；否则命中 brain 词 → brain 链；
//           短消息/纯问候 → simple 链；其余默认 brain（最稳大脑接盘）
const KW_CODING = [
  // 编程
  '代码', '编程', '写个程序', '程序', '脚本', '报错', 'bug', '修复', '函数', '接口',
  'sql', '查询', '数据库', 'excel', 'csv', '表格', '爬虫', '正则', 'shell',
  '命令行', 'git', '部署', 'docker', 'api', 'python', 'javascript', '前端', '后端', '自动化',
  '测试', '调试', '优化性能', '日志分析', '写个函数', '代码审查', 'lint',
  // 数据分析（先生拍板：数据类走 coding）
  '数据', '报表', '统计', '对账', '透视', '销量', '销售数据', '趋势', '归因', '漏斗',
  '留存', 'dau', 'uv', 'gmv', '转化率', '活跃', '订单', '补贴', '营收', '毛利',
];
const KW_BRAIN = [
  '方案', '总结', '报告', '计划', '策划', '复盘', '规划', '观点', '建议', '策略',
  '评估', '对比', '文档', 'review', '回复', '调研', '汇报', '大纲', '思路',
  '研究', '设计', '架构', '写作', '邮件', '故事', '文案', '稿',
];
const KW_SIMPLE = ['你好', '您好', '早上好', '下午好', '晚上好', '在吗', '谢谢', '再见',
  '哈哈', '嗯', '哦', '好的', 'ok', 'hello', 'hi', 'thanks', 'bye'];

function extractText(content) {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content.map(p => (typeof p === 'string' ? p : (p && p.text) || '')).join(' ');
  }
  return '';
}

function lastUserText(body) {
  const msgs = (body && body.messages) || [];
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i] && msgs[i].role === 'user') {
      return extractText(msgs[i].content);
    }
  }
  return '';
}

function hasKw(text, kws) {
  const t = text.toLowerCase();
  return kws.some(kw => t.includes(kw.toLowerCase()));
}

/**
 * 分流决策（v1.1 新增）
 * @returns {string} 目标路由名
 */
function decideRoute(body) {
  const modelName = (body && body.model) || 'main';
  if (modelName !== 'orchestrator') return modelName; // 非 orchestrator：直通

  const text = lastUserText(body).trim();
  log('INFO', `orchestrator decide: text=${text.slice(0, 60)}`);

  // 1. 编程/数据类（先生拍板：SQL/代码/报错/脚本/数据分析 → coding）
  if (hasKw(text, KW_CODING)) return 'coding';

  // 2. 方案/总结类
  if (hasKw(text, KW_BRAIN)) return 'brain';

  // 3. 简短问候 → simple（长消息/闲聊也走 brain 兜底，simple 只接短句）
  if (text.length <= 20 && hasKw(text, KW_SIMPLE)) return 'simple';

  // 4. 默认 brain（稳定大脑接盘）
  return 'brain';
}

// ---------- HTTP 服务 ----------
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);

  if (req.method === 'GET' && (url.pathname === '/healthz' || url.pathname === '/health')) {
    res.writeHead(200, { 'Content-Type': 'text/plain' });
    res.end('ok\n');
    return;
  }

  const auth = req.headers.authorization || '';
  const clientKey = auth.replace(/^Bearer\s+/i, '');
  if (config.gateway_key && clientKey !== config.gateway_key) {
    res.writeHead(401, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: { message: 'invalid gateway key', type: 'auth' } }));
    return;
  }

  if (req.method === 'GET' && url.pathname === '/v1/models') {
    const list = Object.entries(config.routes || {}).map(([name]) => ({ id: name, object: 'model', owned_by: 'gateway' }));
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ object: 'list', data: list }));
    return;
  }

  // v1.2: 健康统计端点（智脑感知侧拉取）
  if (req.method === 'GET' && url.pathname === '/stats') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      gateway: 'model-gateway',
      started_at: new Date(stats.startedAt).toISOString(),
      uptime_s: Math.round((Date.now() - stats.startedAt) / 1000),
      total_requests: stats.totalRequests,
      by_route: stats.byRoute,
      by_channel: stats.byChannel,
      cooldown: Object.fromEntries([...cooldownUntil.entries()].map(([k, v]) => [k, new Date(v).toISOString()])),
      overrides: { updated_at: overrides.updated_at, demoted: overrides.demoted || [], note: overrides.note || '', routes: Object.keys(overrides.routes || {}) },
      effective_routes: Object.keys(effectiveRoutes()),
    }));
    return;
  }

  if (req.method === 'POST' && url.pathname === '/v1/chat/completions') {
    let raw = '';
    for await (const chunk of req) raw += chunk;
    let body;
    try {
      body = JSON.parse(raw);
    } catch (e) {
      res.writeHead(400, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: { message: 'invalid JSON body', type: 'bad_request' } }));
      return;
    }

    // v1.1: orchestrator 分流（v1.2: 路由链支持 route_overrides 热加载覆盖）
    const modelName = decideRoute(body);
    const route = effectiveRoutes()[modelName];

    if (!route) {
      res.writeHead(400, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: { message: `unknown model '${modelName}' (orchestrator resolved)`, type: 'unknown_model' } }));
      return;
    }

    try {
      const result = await tryChain(modelName, route, body);
      if (result.stream) {
        res.writeHead(result.status, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive' });
        const reader = result.stream.getReader();
        const pump = () => reader.read().then(({ done, value }) => {
          if (done) { res.end(); return; }
          res.write(Buffer.from(value));
          pump();
        }).catch(() => res.end());
        pump();
        return;
      }
      res.writeHead(result.status, result.extraHeaders || { 'Content-Type': 'application/json' });
      res.end(result.text ?? JSON.stringify(result.json));
    } catch (err) {
      log('ERROR', `internal: ${err.message}`);
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: { message: `gateway internal error: ${err.message}`, type: 'internal' } }));
    }
    return;
  }

  res.writeHead(404, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ error: { message: 'not found', type: 'not_found' } }));
});

server.listen(PORT, '0.0.0.0', () => {
  log('INFO', `gateway v1.1 listening on 0.0.0.0:${PORT} | routes: ${Object.keys(config.routes || {}).join(', ')}`);
});