#!/usr/bin/env python3
"""
model-router: 多模型智能路由 + 超时切换
零依赖（仅 Python 标准库），QwenPaw 内置 Python 3.11 直接跑

路由规则（维护者 2026-08-27 定）：
  闲聊   → 不路由！当前对话模型直接答（省时省钱）
  图识别 → Agnes → ModelScope → 阿里百炼 → SenseNova → 火山方舟
  代码   → OpenCode-DeepSeek-V4 → 硅基流动 → OpenCode-Qwen3.8-Max(多模态兜底)
  文档   → OpenCode-DeepSeek-V4 → 硅基流动 → OpenCode-Qwen3.8-Max(多模态兜底)
  长文本 → Agnes(1M) → LongCat → OpenCode-Qwen3.8-Max(多模态兜底)

自动触发：识别以下字段自动判断路由功能（纯闲聊不路由）：
  github、技能、项目 → 代码类
  方案            → 文档类
  图片/截图/照片   → 图识别类

用法：
  python router.py route "写个快速排序" --task code
  python router.py auto "一句话"              # 自动识别任务类型并路由；纯闲聊提示不路由
  python router.py chat --provider agnes "你好"
  python router.py image --image 图.png --prompt "描述这张图"
  python router.py providers
"""

import sys
import os
import json
import time
import base64
import re
import subprocess
import urllib.request
import urllib.error

# 自动加载 .env 文件（skill 目录下）
def _load_env():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())

_load_env()

# ===== 供应商配置（2026-08-27 实测）=====
PROVIDERS = {
    "opencode-mimo": {
        "name": "OpenCode-MiMo-V2.5",
        "base_url": "https://opencode.ai/zen/go/v1/chat/completions",
        "key_env": "OPENCODE_API_KEY",
        "model": "mimo-v2.5",
        "max_tokens": 8192,
        "multimodal": True,
        "free": False,
        "timeout": 30,
        "speed_ms": 0,
        "note": "多模态，仅手动调用（chat --provider）",
    },
    "opencode": {
        "name": "OpenCode-DeepSeek-V4",
        "base_url": "https://opencode.ai/zen/go/v1/chat/completions",
        "key_env": "OPENCODE_API_KEY",
        "model": "deepseek-v4-flash",
        "max_tokens": 8192,
        "multimodal": False,
        "free": False,
        "timeout": 30,
        "speed_ms": 0,
        "note": "复杂任务主力",
    },
    "opencode-qwen": {
        "name": "OpenCode-Qwen3.8-Max",
        "base_url": "https://opencode.ai/zen/go/v1/chat/completions",
        "key_env": "OPENCODE_API_KEY",
        "model": "qwen3.8-max",
        "max_tokens": 8192,
        "multimodal": True,
        "free": False,
        "timeout": 30,
        "speed_ms": 0,
        "note": "已退役（2026-09-10 套餐到期，改用 commandcode），仅历史保留",
    },
    "commandcode": {
        "name": "CommandCode-GOAT-DeepSeek",
        "base_url": "https://api.commandcode.ai/provider/v1/chat/completions",
        "key_env": "COMMANDCODE_API_KEY",
        "model": "deepseek/deepseek-v4-flash-fast",
        "max_tokens": 8192,
        "multimodal": False,
        "free": False,
        "timeout": 30,
        "speed_ms": 0,
        "note": "CommandCode GOAT 主力（2026-09-10 替换 OpenCode 套餐）",
    },
    "agnes": {
        "name": "Agnes-2.0-Flash",
        "base_url": "https://apihub.agnes-ai.com/v1/chat/completions",
        "key_env": "AGNES_API_KEY",
        "model": "agnes-2.0-flash",
        "max_tokens": 8192,
        "multimodal": True,
        "free": True,
        "timeout": 10,
        "speed_ms": 1560,
        "note": "1M上下文，多模态，永久免费",
    },
    "modelscope": {
        "name": "ModelScope-Step-3.7-Flash",
        "base_url": "https://api-inference.modelscope.cn/v1/chat/completions",
        "key_env": "MODELSCOPE_API_KEY",
        "model": "stepfun-ai/Step-3.7-Flash",
        "max_tokens": 8192,
        "multimodal": True,
        "free": True,
        "timeout": 10,
        "speed_ms": 1540,
        "note": "图片识别专用",
    },
    "gateway": {
        "name": "ModelGateway-simple(智脑专用)",
        "base_url": "http://<ROUTER_IP>:4100/v1/chat/completions",
        "key_env": "GATEWAY_API_KEY",
        "model": "simple",
        "max_tokens": 8192,
        "multimodal": False,
        "free": True,
        "timeout": 25,
        "speed_ms": 0,
        "note": "智脑 daemon 走软路由网关 simple 链(LongCat→opencode→Agnes)",
    },
    "sensenova": {
        "name": "SenseNova-6.8-Flash-Lite",
        "base_url": "https://token.sensenova.cn/v1/chat/completions",
        "key_env": "SENSENOVA_API_KEY",
        "model": "sensenova-6.8-flash-lite",
        "max_tokens": 8192,
        "multimodal": True,
        "free": True,
        "timeout": 15,
        "speed_ms": 11180,
        "note": "免费但慢，1500次/5h",
    },
    "openrouter": {
        "name": "OpenRouter-MiniMax-M3",
        "base_url": "https://openrouter.ai/api/v1/chat/completions",
        "key_env": "OPENROUTER_API_KEY",
        "model": "minimax/minimax-m3:free",
        "max_tokens": 8192,
        "multimodal": True,
        "free": True,
        "timeout": 15,
        "speed_ms": 1410,
        "note": "免费，多模态",
    },
    "siliconflow": {
        "name": "硅基流动-DeepSeek-V4",
        "base_url": "https://api.siliconflow.cn/v1/chat/completions",
        "key_env": "SILICONFLOW_API_KEY",
        "model": "deepseek-ai/DeepSeek-V4-Flash",
        "max_tokens": 8192,
        "multimodal": False,
        "free": False,
        "timeout": 30,
        "speed_ms": 4900,
        "note": "复杂任务备选",
    },
    "longcat": {
        "name": "LongCat-2.0",
        "base_url": "https://api.longcat.chat/openai/v1/chat/completions",
        "key_env": "LONGCAT_API_KEY",
        "model": "LongCat-2.0",
        "max_tokens": 8192,
        "multimodal": False,
        "free": False,
        "timeout": 20,
        "speed_ms": 2840,
        "note": "不稳定，仅日常闲聊兜底",
    },
    "volcengine": {
        "name": "火山方舟-豆包",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
        "key_env": "ARK_API_KEY",
        "model": "doubao-seed-evolving",
        "max_tokens": 8192,
        "multimodal": True,
        "free": False,
        "timeout": 15,
        "speed_ms": 0,
        "note": "多模态，充值22",
    },
    "bailian": {
        "name": "阿里百炼-Qwen3-VL-Flash",
        "base_url": "https://ws-1npdxwujxdbrfxen.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions",
        "key_env": "BAILIAN_API_KEY",
        "model": "qwen3-vl-flash",
        "max_tokens": 8192,
        "multimodal": True,
        "free": False,
        "timeout": 15,
        "speed_ms": 0,
        "note": "多模态，充值12",
    },
}

# ===== 路由表（2026-08-27 维护者决定版）=====
# 说明：闲聊不路由！对话模型（QwenPaw 选的当前模型）直接答，省时省钱。
# 路由只用于特殊任务：代码/文档/图识别/长文本。
ROUTE_TABLE = {
    "code": {
        "desc": "代码/Agent/SQL/项目/Github",
        "chain": ["commandcode", "siliconflow"],
    },
    "document": {
        "desc": "文档/报告/方案/写作",
        "chain": ["commandcode", "siliconflow"],
    },
    "image": {
        "desc": "图文识别",
        "chain": ["agnes", "modelscope", "bailian", "sensenova", "volcengine"],
    },
    "longtext": {
        "desc": "长文本(>100K)",
        "chain": ["agnes", "longcat", "commandcode"],
    },
}

# ===== 自动触发关键词（仅特殊任务）=====
AUTO_TRIGGERS = {
    "image": ["图片", "截图", "照片", "识别", "扫码", "二维码", "OCR", "看图", "海报", "图像"],
    "code": ["github", "GitHub", "代码", "项目", "技能", "开发", "SQL", "sql", "脚本", "bug", "报错", "拉取", "clone", "仓库", "部署"],
    "document": ["方案", "报告", "文档", "周报", "月报", "总结", "写作", "公文", "策划", "ppt", "PPT"],
}


def detect_task_type(task):
    """根据内容自动判断任务类型。返回 None 表示纯闲聊 → 不路由，当前对话模型直接答"""
    task_lower = task.lower()
    for task_type, keywords in AUTO_TRIGGERS.items():
        for kw in keywords:
            if kw in task or kw.lower() in task_lower:
                return task_type
    # 长文本检测（>100K 字符）
    if len(task) > 100000:
        return "longtext"
    return None


def call_provider(provider_key, messages, max_tokens=None, image_b64=None):
    """调用指定供应商，返回 (success, result, elapsed)
    OpenCode 系列用 curl（Cloudflare 拦截 Python urllib 指纹），其余用 urllib"""
    p = PROVIDERS[provider_key]
    api_key = os.environ.get(p["key_env"])
    if not api_key:
        return False, f"未设置环境变量 {p['key_env']}", 0

    # 构造消息（支持多模态）
    if image_b64 and p["multimodal"]:
        content = [
            {"type": "text", "text": messages[-1]["content"]},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
        ]
        messages = messages[:-1] + [{"role": "user", "content": content}]

    payload = {
        "model": p["model"],
        "messages": messages,
        "max_tokens": max_tokens or p["max_tokens"],
    }

    start = time.time()
    try:
        if "opencode.ai" in p["base_url"]:
            # curl 通道（绕 Cloudflare 指纹拦截）
            body = json.dumps(payload)
            cmd = [
                "curl", "-s", "--connect-timeout", str(p["timeout"]),
                "-X", "POST", p["base_url"],
                "-H", "Content-Type: application/json",
                "-H", f"Authorization: Bearer {api_key}",
                "-d", body
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=p["timeout"] + 5)
            stdout = proc.stdout.strip()
            if not stdout:
                return False, f"curl 空响应 stderr={proc.stderr[:100]}", time.time() - start
            try:
                result = json.loads(stdout)
            except json.JSONDecodeError:
                return False, f"curl 非JSON响应: {stdout[:100]}", time.time() - start
            if "error" in result:
                return False, f"API错误: {json.dumps(result['error'])[:200]}", time.time() - start
            elapsed = time.time() - start
            return True, result, elapsed
        else:
            req = urllib.request.Request(
                p["base_url"],
                data=json.dumps(payload).encode(),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
                },
            )
            resp = urllib.request.urlopen(req, timeout=p["timeout"])
            elapsed = time.time() - start
            result = json.loads(resp.read())
            return True, result, elapsed
    except urllib.error.HTTPError as e:
        elapsed = time.time() - start
        return False, f"HTTP {e.code}: {e.read().decode()[:200]}", elapsed
    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        return False, f"curl 超时({p['timeout']}s)", elapsed
    except Exception as e:
        elapsed = time.time() - start
        return False, str(e), elapsed


def route(task, task_type=None, image_b64=None, max_tokens=None):
    """按路由链依次尝试。task_type=None 时自动判断，纯闲聊返回不路由。"""
    if task_type is None:
        task_type = detect_task_type(task)

    if task_type is None:
        print("💬 纯闲聊 → 不路由")
        print("   路由只用于特殊任务（代码/文档/图识别/长文本）")
        print("   此任务由当前对话模型直接回答，省时省钱 ✅")
        return

    if task_type not in ROUTE_TABLE:
        print(f"❌ 未知任务类型: {task_type}")
        print(f"   可用类型: {', '.join(ROUTE_TABLE.keys())}")
        return

    route_info = ROUTE_TABLE[task_type]
    chain = route_info["chain"]
    print(f"📋 任务: {task[:50]}")
    print(f"📊 类型: {route_info['desc']}")
    print(f"🔗 路由链: {' → '.join(chain)}")
    print()

    messages = [{"role": "user", "content": task}]

    for provider_key in chain:
        p = PROVIDERS[provider_key]
        print(f"⏳ 尝试 {p['name']} (超时{p['timeout']}s)...", end=" ", flush=True)
        success, result, elapsed = call_provider(
            provider_key, messages, max_tokens, image_b64
        )

        if success:
            usage = result.get("usage", {})
            reply = result["choices"][0]["message"]["content"]
            print(f"✅ {elapsed:.2f}s")
            print(f"\n{'='*50}")
            print(f"🎯 命中: {p['name']}")
            print(f"⏱️  耗时: {elapsed:.2f}s")
            print(f"📊 Token: prompt={usage.get('prompt_tokens','?')} completion={usage.get('completion_tokens','?')}")
            print(f"{'='*50}")
            print(f"\n{reply}")
            return
        else:
            print(f"❌ {elapsed:.2f}s - {result[:80]}")

    print(f"\n❌ 路由链全部失败: {' → '.join(chain)}")


def list_providers():
    """列出所有供应商及 Key 配置状态"""
    print("=== 模型路由供应商 ===\n")
    print(f"{'供应商':<16} {'模型':<30} {'多模态':<8} {'免费':<6} {'Key状态':<10} {'速率':<10}")
    print("-" * 95)
    for key, p in PROVIDERS.items():
        has_key = "✅" if os.environ.get(p["key_env"]) else "❌"
        speed = f"{p['speed_ms']}ms" if p['speed_ms'] > 0 else "未测"
        print(f"{key:<16} {p['name']:<30} {'✅' if p['multimodal'] else '❌':<8} {'✅' if p['free'] else '❌':<6} {has_key:<10} {speed:<10} {p['note']}")
    print(f"\n路由规则:")
    for task_type, info in ROUTE_TABLE.items():
        print(f"  {task_type:<10} → {' → '.join(info['chain'])}")
    print(f"\n自动触发:")
    for task_type, keywords in AUTO_TRIGGERS.items():
        print(f"  {task_type:<10} ← {'、'.join(keywords[:6])}...")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "providers":
        list_providers()
    elif cmd in ("route", "auto"):
        if len(sys.argv) < 3:
            print("用法: python router.py route <任务描述> [--task chat|code|document|image|longtext]")
            sys.exit(1)
        task = sys.argv[2]
        task_type = None
        if "--task" in sys.argv:
            idx = sys.argv.index("--task")
            if idx + 1 < len(sys.argv):
                task_type = sys.argv[idx + 1]
        elif cmd == "auto":
            task_type = detect_task_type(task)
        route(task, task_type)
    elif cmd == "chat":
        if len(sys.argv) < 4:
            print("用法: python router.py chat --provider <供应商> <消息>")
            sys.exit(1)
        provider = sys.argv[3] if sys.argv[2] == "--provider" else sys.argv[2]
        msg = sys.argv[3] if sys.argv[2] != "--provider" else sys.argv[4]
        success, result, elapsed = call_provider(provider, [{"role": "user", "content": msg}])
        if success:
            print(result["choices"][0]["message"]["content"])
        else:
            # 2026-09-10 事故：此处原先只 print 不退出，returncode 仍为 0，
            # 调用方（judge/profile_daemon）会把 "❌ HTTP 503 ..." 当模型回复，
            # 进而在 parse 环节静默产出假判定（keep=False）并写进记忆库。
            # 失败必须非零退出，让调用方走「降级/重试」而不是当成正常输出。
            print(f"❌ {result}", file=sys.stderr)
            sys.exit(1)
    elif cmd == "image":
        if len(sys.argv) < 4:
            print("用法: python router.py image --image <图片路径> --prompt <描述>")
            sys.exit(1)
        image_path = sys.argv[3] if sys.argv[2] == "--image" else None
        prompt = sys.argv[5] if len(sys.argv) > 5 else "描述这张图"
        if image_path and os.path.exists(image_path):
            with open(image_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode()
            route(prompt, "image", img_b64)
        else:
            print(f"❌ 图片不存在: {image_path}")
    else:
        print(f"未知命令: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()