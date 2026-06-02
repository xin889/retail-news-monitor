# 外部动态监控雷达 —— 大商超事业群

外部动态监控雷达 —— 大商超事业群，是一个零售与消费竞争情报看板 MVP，用于帮助京东大商超事业群每日了解外部平台竞对、零售商竞对、品类政策 / 热点 / 品牌动态。

项目使用 GitHub Pages 托管静态页面，使用 GitHub Actions 定时运行 Python 脚本刷新公开 RSS 与 Google News RSS 新闻，并把轻量数据写入 `docs/news.json`。不使用数据库、不使用 OpenAI API、不使用付费新闻 API。

## 功能概览

- 平台竞对：淘宝天猫、抖音电商、拼多多、美团。
- 零售商：Costco、山姆、小象超市、朴朴超市、叮咚买菜、奥乐齐、盒马 NB、快乐猴、盒马、沃尔玛、胖东来、永辉、Ole'、大润发、物美。
- 品类洞察：酒类、母婴、水饮冲调、家庭清洁 / 纸品、个人护理、粮油调味、玩具乐器、休闲食品、宠物。
- 支持全局搜索、主板块切换、对象筛选、维度筛选、重要性排序、高重要性过滤。
- 无 `DEEPSEEK_API_KEY` 时自动使用规则版解读。
- 有 `DEEPSEEK_API_KEY` 时调用 DeepSeek 做分类、评分与商业解读。

## 启用 GitHub Pages

1. 打开 GitHub 仓库。
2. 进入 `Settings`。
3. 进入 `Pages`。
4. `Source` 选择 `Deploy from a branch`。
5. 分支选择 `main`，目录选择 `/docs`。
6. 保存后等待 GitHub Pages 完成发布。

## 手动刷新新闻

1. 打开仓库的 `Actions`。
2. 选择 `Refresh Retail Intelligence News`。
3. 点击 `Run workflow`。

也可以在本地运行：

```bash
python scripts/fetch_news.py
```

## 配置 DeepSeek API Key

1. 打开 GitHub 仓库。
2. 进入 `Settings`。
3. 进入 `Secrets and variables` → `Actions`。
4. 点击 `New repository secret`。
5. `Name` 填写 `DEEPSEEK_API_KEY`。
6. `Value` 填写你自己的 DeepSeek API Key。

不要把 API Key 写入代码，也不要提交到仓库。

## 修改 DeepSeek 模型

默认模型为 `deepseek-v4-flash`。

如需修改，编辑 `.github/workflows/refresh-news.yml` 中的环境变量：

```yaml
DEEPSEEK_MODEL: deepseek-v4-flash
```

本地运行时也可以设置环境变量：

```bash
DEEPSEEK_MODEL=deepseek-v4-flash python scripts/fetch_news.py
```

## 增加监控对象

编辑 `config/watchlist.json`。

- 平台和零售商放入 `platforms` 或 `retailers`。
- 品类放入 `categories`。
- 每个对象都可以配置 `aliases`、`dimensions`、`queries`、`feeds` 和 `priority`。
- `feeds` 可以为空，也可以添加官方 RSS 或公开页面 RSS 地址。

## 调整分类和维度

编辑 `config/taxonomy.json`。

可调整：

- `sections`
- `platform_dimensions`
- `retailer_dimensions`
- `category_tabs`
- `category_subtabs`
- `global_categories`
- `importance_keywords`
- `noise_keywords`
- `source_quality_rules`

## 方案边界

- 不使用数据库。
- 不使用付费新闻 API。
- 不使用 OpenAI API。
- Python 仅使用标准库。
- 前端仅使用原生 HTML、CSS、JavaScript。
- 如果未配置 DeepSeek API Key，则使用规则版 AI 解读。
- 新闻覆盖依赖公开 RSS 与 Google News RSS，不保证 100% 全量。
- 如需更高覆盖率，后续可接入新闻 API、数据库、企业内部数据源、更多官方 RSS 或网页抓取。
