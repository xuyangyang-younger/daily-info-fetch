# Daily Info Fetch — 每日新闻摘要推送

自动抓取国际金融、AI、科技新闻，生成中文摘要，通过微信推送。

## 架构

```
[System Crontab 08:30 / 21:00 BJT]
        ↓
[daily_news.py]
  ├── 抓取新闻 (HN API + TechCrunch RSS + 36氪 RSS)
  ├── 分类 (金融 / AI / 科技)
  ├── 调用 Hermes LLM API 生成中文摘要
  └── 通过 Hermes send_weixin_direct 推送到微信
```

## 推送内容

每次推送 5-8 条精选新闻：

- 💰 **金融市场** — 股市、央行、经济、加密货币
- 🤖 **AI 前沿** — 新模型、研究突破、AI 产品
- 🔬 **科技动态** — 科技公司、产品发布、行业趋势

## 新闻来源

| 来源 | 类型 | 覆盖 |
|------|------|------|
| [Hacker News](https://news.ycombinator.com) | Firebase API | 科技/AI/创业 |
| [TechCrunch](https://techcrunch.com) | RSS Feed | 科技/融资/产品 |
| [36氪](https://36kr.com) | RSS Feed | 中国科技/创业/金融 |

## 服务器部署

### 服务器信息

- **IP**: `47.116.69.234`
- **OS**: Ubuntu 24.04.4 LTS
- **时区**: Asia/Shanghai (CST +0800)
- **Hermes**: v0.8.0，API 端口 8000

### 文件位置

```
/root/daily-news-digest/
├── daily_news.py      # 主脚本
└── logs/
    ├── daily_news.log # 运行日志
    └── cron.log       # Cron 输出日志
```

### 定时任务

```
30 8  * * *  早间新闻 (08:30 BJT)
0  21 * * *  晚间新闻 (21:00 BJT)
```

## 运维命令

```bash
# 查看定时任务
ssh root@47.116.69.234 "crontab -l | grep -A1 Daily"

# 手动触发一次
ssh root@47.116.69.234 "/root/.hermes/hermes-agent/venv/bin/python3 /root/daily-news-digest/daily_news.py"

# 查看运行日志
ssh root@47.116.69.234 "tail -50 /root/daily-news-digest/logs/daily_news.log"

# 查看 Cron 日志
ssh root@47.116.69.234 "tail -20 /root/daily-news-digest/logs/cron.log"

# 暂停推送（注释 crontab）
ssh root@47.116.69.234 "crontab -l | sed 's/^30 8/#30 8/;s/^0 21/#0 21/' | crontab -"

# 恢复推送
ssh root@47.116.69.234 "crontab -l | sed 's/^#30 8/30 8/;s/^#0 21/0 21/' | crontab -"
```

## 技术栈

- **Python 3.12** — 主脚本（requests + xml.etree）
- **Hermes Agent v0.8.0** — LLM API（GitHub Copilot 后端）+ 微信发送（iLink Bot API）
- **System Crontab** — 定时调度

## 配置说明

脚本通过环境变量配置，默认值已内置：

| 变量 | 用途 | 默认值 |
|------|------|--------|
| `HERMES_API_URL` | Hermes LLM API | `http://localhost:8000/v1/chat/completions` |
| `HERMES_API_KEY` | API 密钥 | 已配置 |
| `WEIXIN_TOKEN` | iLink Bot Token | 已配置 |
| `WEIXIN_CHAT_ID` | 微信推送目标 | 已配置 |
| `WEIXIN_ACCOUNT_ID` | Bot 账号 ID | 已配置 |
