# WeChat Search - 微信公众号文章采集工具

[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![Playwright](https://img.shields.io/badge/playwright-1.40+-green.svg)](https://playwright.dev/)
[![License](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

> 自动化采集微信公众号文章，一键转换为 Obsidian 兼容的 Markdown 格式

---

## 🎯 功能特性

- **🔍 双模式搜索**
  - 按文章标题关键词搜索
  - 按公众号名称精确搜索

- **🖼️ 智能图片处理**
  - 自动下载文章配图
  - 支持 Base64 内嵌（单文件便携）或本地引用（节省空间）

- **📝 Obsidian 原生兼容**
  - 生成 YAML Frontmatter（标题、作者、日期、来源）
  - 干净的 Markdown 格式，无广告、无冗余样式

- **🤖 零 Token 消耗**
  - 搜索和采集阶段完全自动化
  - 仅在最后的 Markdown 转换阶段调用 LLM

- **🔒 安全合规**
  - 通过 CDP 连接真实浏览器，模拟人工操作
  - 支持验证码手动处理，不依赖第三方识别服务

---

## 📦 安装

### 环境要求

- Python 3.8+
- Chrome / Edge / Chromium 浏览器（已安装）

### 步骤

```bash
# 1. 克隆或下载本项目到本地
cd /path/to/wechat-search

# 2. 安装 Python 依赖
pip install -r requirements.txt

# 3. 安装 Playwright 浏览器
playwright install chromium
```

---

## ⚙️ 配置

编辑 `config.yaml` 文件：

```yaml
# 输出目录（MD 文件和图片保存位置）
output_dir: "D:/MyObsidian/vault/微信文章"

# 图片附件子目录名
attachments_dir: "attachments"

# Chrome DevTools Protocol 连接地址
cdp_endpoint: "http://127.0.0.1:9222"

# 搜索配置
pages_to_search: 20        # 最大翻页数
max_results: 60            # 最多采集文章数（0 = 不限制）
fetch_interval: 2          # 页面访问间隔（秒）

# 图片处理方式
embed_images: true         # true = Base64 内嵌，false = 本地引用
```

### Chrome 启动参数

为保持登录状态，建议以调试模式启动 Chrome：

```bash
# Windows
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222

# macOS
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222

# Linux
google-chrome --remote-debugging-port=9222
```

---

## 🚀 使用方式

### 方式一：与 AI 助手对话（推荐）

直接告诉你的 AI 助手：

> "帮我采集公众号『人民日报』最近的文章"

> "搜索关于『人工智能』的微信文章"

AI 会自动调用本工具完成全流程。

### 方式二：命令行手动执行

```bash
cd /path/to/wechat-search/scripts

# 阶段 1：搜索文章 URL
python search.py --mode account --keyword "人民日报" --config ../config.yaml

# 阶段 2：下载文章内容
python fetch.py --config ../config.yaml

# 阶段 3：转换为 Markdown
python convert.py --config ../config.yaml
```

**参数说明：**
- `--mode`：`title`（标题搜索）或 `account`（公众号搜索）
- `--keyword`：搜索关键词
- `--config`：配置文件路径

---

## 📂 输出结构

```
output_dir/
├── urls.json                 # 采集到的文章链接清单
├── cleaned/
│   ├── 1.json                # 文章原始数据
│   ├── 2.json
│   └── ...
├── attachments/              # 图片附件目录（若 embed_images=false）
│   ├── img_xxx_1.png
│   └── ...
├── 2026-04-15-文章标题A.md   # 生成的 Markdown 文件
├── 2026-04-15-文章标题B.md
└── ...
```

### Markdown 文件示例

```markdown
---
title: 文章标题
author: 公众号名称
date: 2026-04-15
source: https://mp.weixin.qq.com/s/...
---

# 文章标题

正文内容...

![图片描述](attachments/img_xxx_1.png)
```

---

## ⚠️ 注意事项

1. **验证码处理**
   - 搜狗搜索可能会触发验证码
   - 浏览器窗口会显示验证码页面
   - 在浏览器中完成验证后，在终端按 Enter 继续

2. **反爬保护**
   - 已内置随机延迟和页面访问间隔
   - 建议根据实际需求设置 `max_results`，避免过度采集

3. **版权声明**
   - 本工具仅供个人学习和研究使用
   - 请尊重原作者版权，勿用于商业用途

4. **风险提示**
   - 频繁大量采集可能导致搜狗临时封禁 IP
   - 建议控制采集频率，必要时更换网络环境

---

## 🛠️ 技术架构

| 组件 | 用途 |
|------|------|
| Playwright | 浏览器自动化、CDP 通信 |
| BeautifulSoup | HTML 解析 |
| Markdownify | HTML 转 Markdown |
| PyYAML | 配置管理 |

### 工作流程

```
用户输入 → 搜索脚本 → 采集 URL → 下载脚本 → 获取内容 → 转换脚本 → Markdown 输出
```

---

## 📝 更新日志

### v1.0.0
- ✨ 支持标题和公众号双模式搜索
- ✨ 自动下载文章图片
- ✨ 生成 Obsidian 兼容的 Markdown
- ✨ CDP 模式连接 Chrome，支持验证码手动处理
