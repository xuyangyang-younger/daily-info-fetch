# Daily Info Fetch — 每日新闻摘要推送

自动抓取国际金融、AI、科技新闻，经 LLM 生成中文摘要，通过 Hermes 的微信 Bot 通道推送到个人微信。

> 每日早 08:30 / 晚 21:00（北京时间）各推送一次。

---

## 功能概述

- 🌐 **多源并行抓取**：29 个信息源（HN API + RSS + GitHub Trending HTML），单源失败不阻塞
- 🏷️ **自动分类**：关键词归入 `finance` / `ai` / `tech` / `world` 四大板块
- ⚖️ **权重排序**：源权重 × 新鲜度 × 热度 + **交叉源奖励** + 关键词加分 × 板块系数
- 🔀 **交叉验证去重**：标题相似度聚类（英文 token + 中文 bigram Jaccard），多源命中的事件自动加权
- 🧠 **LLM 摘要**：Hermes Agent 根据评分挑选 5–9 条精选，附原始来源列表
- 📱 **微信投递**：Hermes iLink Bot adapter (`send_weixin_direct`)
- ⏰ **定时调度**：Hermes 内置 cron scheduler（无需系统 crontab）

---

## 架构

```
┌──────────────────────────── 服务器 47.116.69.234 ────────────────────────────┐
│                                                                              │
│   Hermes Cron Scheduler                                                      │
│     ├─ job f5e5656b309b  (30 8 * * *)  → 早间                                │
│     └─ job 72cab2499ed2  (0 21 * * *)  → 晚间                                │
│            │                                                                 │
│            │ prompt: 用 terminal 工具运行 daily_news.py --stdout             │
│            ▼                                                                 │
│   Hermes Agent (GPT-5 / Claude backend)                                      │
│            │                                                                 │
│            ▼                                                                 │
│   /root/daily-news-digest/daily_news.py                                      │
│     ①  抓取:  HN API  +  TechCrunch RSS  +  36Kr RSS   (35+ 条)              │
│     ②  分类:  finance / ai / tech  (关键词匹配)                              │
│     ③  摘要:  POST Hermes /v1/chat/completions  → 700-2000 字中文            │
│     ④  投递:  send_weixin_direct(chat_id=…@im.wechat, message=…)             │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
                                         │
                                         ▼
                           📱 微信 (o9cq80wJs4pU5Dbx0YV-5anUXC04)
```

---

## 推送内容示例

```
📰 早间新闻速递 | 2026-04-17

━━━ 💰 金融市场 ━━━

1️⃣ 【美联储放鸽，年内预期两降】
美联储维持 5.25–5.50% 利率，鲍威尔暗示年内仍有两次降息空间……
📎 WSJ Markets · CNBC · 华尔街见闻 · MarketWatch

━━━ 🤖 AI 前沿 ━━━

2️⃣ 【OpenAI 发布 Codex 全平台更新】
新版 Codex 桌面端加入 computer-use、图像生成、插件能力……
📎 OpenAI Blog · Hacker News

━━━ 🔬 科技动态 ━━━

3️⃣ 【Bluesky 遭遇持续 DDoS】
Bluesky 社交平台被攻击近 24 小时……
📎 Hacker News · The Verge

━━━ 🌍 世界要闻 ━━━

4️⃣ 【…】
📎 BBC World · NYT HomePage

📊 本期覆盖多源交叉验证 | 共 8 条精选
```

---

## 新闻来源（29 个，并行抓取）

| 板块 | 来源 | 源权重 |
|------|------|:---:|
| 💰 金融 | WSJ Markets / WSJ Business | 1.2 |
|  | CNBC Top / CNBC Finance | 1.1 |
|  | 华尔街见闻 | 1.1 |
|  | MarketWatch / Yahoo Finance / Investing.com | 0.9–1.0 |
| 🤖 AI | OpenAI Blog（一手） | **1.5** |
|  | Google AI Blog / DeepMind（一手） | 1.4 |
|  | MIT Tech Review AI | 1.2 |
|  | arXiv cs.AI / cs.LG / cs.CL | 1.0–1.1 |
|  | 量子位 | 1.0 |
| 🔬 科技 | BBC Tech | 1.1 |
|  | Hacker News / TechCrunch / The Verge / Ars Technica / Wired / 36Kr | 1.0 |
|  | InfoQ / 爱范儿 / GitHub Trending | 0.9 |
|  | 少数派 | 0.8 |
| 🌍 世界 | BBC World / NYT HomePage | 1.2 |

**评分公式**：`score = (source_weight × freshness × engagement + cross_source_bonus + keyword_boost) × category_boost`

- **freshness**: `exp(-hours_old / 24)`（24h 半衰期）
- **engagement**: HN 使用 `log10(score + 2×comments)`，其余 = 1.0
- **cross_source_bonus**: 多源命中同一事件时 +0.6 × (N−1) — 最重要的"真热度"信号
- **keyword_boost**: 命中 `OpenAI/Fed/英伟达/发布/降息/收购` 等热词 +0.3
- **category_boost**: AI × 1.2 / 金融 × 1.1 / 科技 × 1.0 / 世界 × 0.9

**去重**: 标题相似度 ≥ 0.5 的聚类合并，来源字段合并（英文 token + 中文 bigram 双重 Jaccard）。

---

## 本地仓库结构

```
daily-info-fetch/
├── daily_news.py   # 主脚本：抓取 + 摘要 + 发送
├── README.md       # 本文档
└── .gitignore
```

---

## 服务器部署

### 文件位置

```
/root/daily-news-digest/
├── daily_news.py              # 主脚本 (本仓库同步)
└── logs/
    └── daily_news.log         # 运行日志

/root/.hermes/
├── hermes-agent/              # Hermes Agent 源码 + venv
├── weixin/accounts/
│   ├── b28d654c09d9@im.bot.context-tokens.json   # ← ⚠️ 会过期，见下
│   ├── b28d654c09d9@im.bot.sync.json
│   └── b28d654c09d9@im.bot.json
└── cron/
    ├── jobs/                  # cron job 定义
    └── output/                # 每次执行的 stdout/stderr
```

### 运行环境

- 使用 Hermes 自带 venv：`/root/.hermes/hermes-agent/venv/bin/python3`
- 因为脚本 `sys.path.insert(0, "/root/.hermes/hermes-agent")` 以便 `import gateway.platforms.weixin`

### 定时调度（Hermes Cron，不是系统 crontab）

```
job f5e5656b309b  →  30 8  * * *   (早间 08:30 BJT)
job 72cab2499ed2  →  0  21 * * *   (晚间 21:00 BJT)
```

Job 的 prompt 让 Hermes Agent 用 `terminal` 工具跑 `daily_news.py --stdout`，并把输出通过 `deliver: weixin:…@im.wechat` 推送。

---

## ⚠️ 关键运维知识：context token 刷新

微信 iLink 协议要求每条发送请求携带有效的 `context_token`。Token 会因会话过期或 bot 重启等原因失效，此时：

- `send_weixin_direct()` 仍然会返回 `success: True`（因为 HTTP 200）
- 但 iLink 响应 body 是 `{"ret": -2}`，用户**收不到消息**

**恢复方法：在微信里给 bot (`b28d654c09d9@im.bot`) 主动发一条消息**（任意内容，比如 `ping`）。Hermes 的长轮询会捕获到并刷新：

```
/root/.hermes/weixin/accounts/b28d654c09d9@im.bot.context-tokens.json
```

文件 `mtime` 更新后，下一次推送就能正常送达。

**排查方法**：如果怀疑没收到，检查 token 文件修改时间：

```bash
ssh root@47.116.69.234 "stat /root/.hermes/weixin/accounts/b28d654c09d9@im.bot.context-tokens.json"
```

如果 mtime 距今已经很久（> 几天），大概率需要手动刷新。

---

## 常用运维命令

```bash
# ── 手动触发一次完整流程（抓取 + 摘要 + 发送）──
ssh root@47.116.69.234 \
  "/root/.hermes/hermes-agent/venv/bin/python3 /root/daily-news-digest/daily_news.py"

# ── 查看脚本运行日志 ──
ssh root@47.116.69.234 "tail -80 /root/daily-news-digest/logs/daily_news.log"

# ── 查看 Hermes Cron jobs ──
ssh root@47.116.69.234 \
  "curl -s -H 'Authorization: Bearer REDACTED_HERMES_KEY' \
   http://localhost:8000/api/jobs | python3 -m json.tool"

# ── 手动触发某个 cron job ──
ssh root@47.116.69.234 \
  "curl -s -X POST -H 'Authorization: Bearer REDACTED_HERMES_KEY' \
   http://localhost:8000/api/jobs/f5e5656b309b/run"

# ── 查看 cron 最近一次输出 ──
ssh root@47.116.69.234 "ls -lt /root/.hermes/cron/output/f5e5656b309b/ | head"

# ── 检查 context token 新鲜度 ──
ssh root@47.116.69.234 \
  "stat /root/.hermes/weixin/accounts/b28d654c09d9@im.bot.context-tokens.json"
```

---

## 同步代码到服务器

本仓库是"源码之家"；任何脚本改动的标准流程：

```bash
# 1. 本地改、提交、推 GitHub
git add daily_news.py
git commit -m "tweak: …"
git push

# 2. 服务器拉最新版（或 scp 覆盖）
ssh root@47.116.69.234 \
  "cd /root/daily-news-digest && curl -sL \
   https://raw.githubusercontent.com/xuyangyang-younger/daily-info-fetch/main/daily_news.py \
   -o daily_news.py"

# 3. 验证
ssh root@47.116.69.234 \
  "/root/.hermes/hermes-agent/venv/bin/python3 /root/daily-news-digest/daily_news.py"
```

---

## 技术栈

| 组件 | 版本 / 说明 |
|------|-------------|
| Python | 3.12（使用 Hermes venv） |
| requests | HTTP 客户端，抓取 RSS / HN API |
| xml.etree | 标准库 RSS 解析（无第三方依赖） |
| Hermes Agent | v0.8.0，提供 LLM API + 微信 Bot 通道 |
| iLink Bot API | 微信官方开放能力，`https://ilinkai.weixin.qq.com` |

脚本本身**零额外第三方依赖**（只用 `requests` + 标准库），易于部署。

---

## 配置变量

脚本通过环境变量配置，默认值已内置（生产值）：

| 变量 | 用途 |
|------|------|
| `HERMES_API_URL` | Hermes `/v1/chat/completions` 端点 |
| `HERMES_API_KEY` | Hermes Bearer token |
| `WEIXIN_TOKEN` | iLink Bot token（形如 `xxx@im.bot:xxxxxx`） |
| `WEIXIN_CHAT_ID` | 目标用户/群（形如 `xxx@im.wechat`） |
| `WEIXIN_ACCOUNT_ID` | Bot 账号 ID（形如 `xxx@im.bot`） |

---

## 故障排查速查表

| 现象 | 可能原因 | 解决方法 |
|------|----------|----------|
| 日志 `success: True` 但微信没消息 | context token 过期，iLink 返回 `ret:-2` | 在微信给 bot 发一条消息即可 |
| LLM 摘要为空 / 超时 | Hermes agent 模型繁忙 | 重试；检查 `http://localhost:8000/health` |
| `HN: fetched 0 stories` | Firebase 被墙 / 网络抖动 | 重试；其它源仍可独立工作 |
| `36Kr RSS fetch failed` | RSS 结构变化 / 反爬 | 暂时只看 HN + TechCrunch，不影响整体 |
| cron 未触发 | Hermes gateway 服务未运行 | `systemctl --user status hermes-gateway` |

---

## License

个人项目，未开源协议声明。
