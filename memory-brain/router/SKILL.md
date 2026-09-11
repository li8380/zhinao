---
name: model-router
description: ⚠️ 已废弃（2026-09-04）。图识别功能已并入 vision-multimodal，代码/文档路由不再需要。保留文件仅作历史参考，请勿继续使用。
whenToUse: 不再触发。请使用 vision-multimodal 替代。
---

# ⚠️ model-router 已废弃

> **2026-09-04 维护者决定**：图识别功能并入 `vision-multimodal`，代码/文档路由不再需要。本技能保留仅作历史参考，**请勿继续使用**。

## 替代方案

| 原功能 | 新方案 |
|--------|--------|
| 图识别路由链 | → `vision-multimodal` 的 `route` 命令 |
| 代码/文档路由 | → 当前对话模型直接处理（DeepSeek V4 Flash 够用） |
| 长文本路由 | → 当前对话模型直接处理 |
| 多模型对比 | → `vision-multimodal` 的 `compare` 命令 |

---

# 【历史文档，仅供参考】

零依赖多模型路由：按任务类型自动选择最优模型，失败/超时自动切换。OpenCode 系列走 curl 通道（绕 Cloudflare）。

## 核心原则：闲聊不路由 💬

**日常闲聊由 QwenPaw 当前对话模型直接回答，不经过路由**——省时省钱，避免绕路转发。

路由只用于**特殊任务**：代码/文档/图识别/长文本。

## 路由规则（维护者 2026-08-27 拍板版）

| 任务类型 | 路由链 | 说明 |
|----------|--------|------|
| 代码/Agent/SQL/项目/Github | OpenCode-V4 → 硅基流动 → OpenCode-Qwen3.8-Max | Qwen3.8-Max 多模态兜底 |
| 文档/报告/方案/写作 | OpenCode-V4 → 硅基流动 → OpenCode-Qwen3.8-Max | Qwen3.8-Max 多模态兜底 |
| 图文识别 | Agnes → ModelScope → 阿里百炼 → SenseNova → 火山方舟 | 免费+付费兜底 |
| 长文本(>100K) | Agnes → LongCat → OpenCode-Qwen3.8-Max | Agnes 1M 上下文 |
| ~~闲聊~~ | ~~不路由~~ | 当前对话模型直接答 |

## 自动触发（仅特殊任务）

识别任务内容中的关键词自动路由；**无关键词默认不路由**（= 闲聊，当前模型直接答）：

| 触发词 | 路由到 |
|--------|--------|
| 图片、截图、照片、识别、扫码、二维码、OCR、看图、海报 | 图识别 |
| github、GitHub、代码、项目、技能、开发、SQL、脚本、bug、报错、拉取、clone、仓库、部署 | 代码类 |
| 方案、报告、文档、周报、月报、总结、写作、公文、策划、ppt | 文档类 |
| >100K 字符 | 长文本 |
| 无关键词 | **不路由**（闲聊） |

## 供应商配置（2026-08-27 实测）

| 供应商 | 模型 | 多模态 | 免费 | 备注 |
|--------|------|--------|------|------|
| OpenCode-MiMo-V2.5 | mimo-v2.5 | ✅ | ❌ | 闲聊主力 |
| OpenCode-DeepSeek-V4 | deepseek-v4-flash | ❌ | ❌ | 复杂任务主力 |
| OpenCode-Qwen3.8-Max | qwen3.8-max | ✅ | ❌ | 多模态兜底 |
| Agnes | agnes-2.0-flash | ✅ | ✅ | 1M上下文 |
| ModelScope | Step-3.7-Flash | ✅ | ✅ | 图片识别 |
| OpenRouter | MiniMax-M3 | ✅ | ✅ | 免费多模态 |
| SenseNova | sensenova-6.8-flash-lite | ✅ | ✅ | 免费但慢 |
| 硅基流动 | DeepSeek-V4-Flash | ❌ | ❌ | 复杂任务备选 |
| LongCat | LongCat-2.0 | ❌ | ❌ | 不稳定，仅闲聊兜底 |
| 火山方舟 | doubao-seed-evolving | ✅ | ❌ | 多模态 |
| 阿里百炼 | Qwen3-VL-Flash | ✅ | ❌ | 多模态，最快 |

## 使用方式

### 1. 自动路由（推荐，仅特殊任务；闲聊不路由）
```bash
python skills/model-router/router.py auto "帮我拉取github项目"   # 自动识别为代码
python skills/model-router/router.py auto "写个方案"              # 自动识别为文档
python skills/model-router/router.py auto "看看这张图"            # 自动识别为图识别
python skills/model-router/router.py auto "今天天气不错"          # 纯闲聊 → 不路由
```

### 2. 手动指定类型
```bash
python skills/model-router/router.py route "写个快速排序" --task code
python skills/model-router/router.py route "写周报" --task document
```

### 3. 图文识别
```bash
python skills/model-router/router.py image --image 图.png --prompt "提取图中文字"
```

### 4. 指定供应商
```bash
python skills/model-router/router.py chat --provider agnes "你好"
```

### 5. 查看供应商状态
```bash
python skills/model-router/router.py providers
```

## 与其他工具协同

- model-router 是辅助路由通道，可与任何技能同时使用（如 analytics-report 出报告时自动路由到 code/document 链）
- 调用时自动带上记忆上下文与人设（助手/维护者画像），不丢失对话状态

## 技术说明

- **OpenCode 系列必须走 curl 通道**：OpenCode 的 Cloudflare 拦截 Python urllib 指纹，router.py 已自动处理（检测到 opencode.ai 域名自动切 curl）
- Key 配置在 `.env` 文件，无需手动输入
- 超时自动切换下一个模型，路由链全部失败才报错
