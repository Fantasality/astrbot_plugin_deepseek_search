# DeepSeek 官方搜索接入

把 DeepSeek 官方新上线的 **Search** 与 **Fetch** 接口接入 AstrBot，让 Bot 具备
「实时联网搜索 + 读取网页原文」的能力。

- 官方文档：<https://api-docs.deepseek.com/zh-cn/guides/search>
- 接口：`POST /v1/tools/search`、`POST /v1/tools/fetch`

## 能力一览

| 类型 | 名称 | 说明 |
|---|---|---|
| LLM 工具 | `web_search_deepseek` | 联网检索，返回带 `index` 的结构化结果，便于模型引用 |
| LLM 工具 | `deepseek_fetch_web_page` | 抓取单个网页并转成 Markdown 正文 |
| 命令 | `/dsearch 关键词 [--count 1~50]` | 人工快速搜索 |
| 命令 | `/dfetch https://…` | 人工抓取网页 |
| 命令 | `/dshelp` | 用法与计费说明 |

`/dsearch` 的别名：`/ds`、`/dssearch`；`/dfetch` 的别名：`/dsf`、`/dsfetch`。

## 安装

1. 把整个 `astrbot_plugin_deepseek_search` 目录放到 AstrBot 的
   `data/plugins/` 下，或在 WebUI「插件管理」里上传 zip 包。
2. 在插件配置里填写 `api_key`（或用下面说的复用方式）。
3. 重载插件。

## 密钥配置

两种方式，任选其一：

1. **直接填写**：在插件配置 `api_key` 填写。支持多个密钥，用英文逗号或换行分隔；
   插件会轮询使用，并在 `401 / 402 / 429` 时自动切换到下一个。
2. **复用 AstrBot 已有密钥**：保持 `api_key` 为空，开启 `fallback_to_astrbot_key`（默认开启）。
   插件会自动在 `provider_sources` 中查找 `provider=deepseek` 或 `api_base` 指向
   `api.deepseek.com` 的供应商，直接复用其密钥。

## 官方搜索语法

`query` 中的语法会原样传给搜索服务，可组合使用：

| 运算符 | 示例 | 说明 |
|---|---|---|
| `site:` | `DeepSeek site:github.com` | 限定站点；多个用 `OR` 连接 |
| `-site:` | `DeepSeek -site:example.com` | 排除站点 |
| `-` | `DeepSeek -广告` | 排除包含该词的网页 |
| `-"..."` | `DeepSeek -"release notes"` | 排除包含该短语的网页 |
| `"..."` | `DeepSeek "精确短语"` | 精确匹配短语 |
| `lang:` | `DeepSeek lang:zh,en` | 限定语言 |
| `after:` | `DeepSeek after:2025-01-01` | 限定发布日期下限 |
| `before:` | `DeepSeek before:2026-06-01` | 限定发布日期上限 |

工具的 `site` / `exclude_site` / `lang` / `after` / `before` / `exact` 参数会自动
拼成上述语法，模型不必自己构造。

## 返回字段

Search 每条结果包含：

| 字段 | 说明 |
|---|---|
| `rerank_score` | 0~3，综合相关性/权威性/时效性，≥1.5 为优质 |
| `title` / `url` | 标题与链接 |
| `snippet` | 100~200 字符短摘要，适合展示 |
| `summary` | 600~800 字符长摘要，适合给模型读 |
| `content` | 网页正文，仅 `need_content=true` 时有值 |
| `site_name` / `site_icon` | 站点名与图标 |
| `date` | UTC RFC 3339 发布时间，未知为 `null` |
| `authority` | 0~4 权威度（`-1` 未知），≥3 为极权威 |

## 计费与限速

- Search、Fetch 均按**成功请求次数**计费：各 **5 元 / 1000 次**。
- 只有 HTTP 200 且产生结果才计费：搜索无结果、请求失败、超时都不计费。
- `count` 只影响返回条数，不影响单次价格。
- 账号级 QPS：Search 5、Fetch 5，两者独立；Fetch 另有目标域名级限速。

## 错误处理

官方错误体为 OpenAI 兼容格式：

```json
{"error": {"message": "...", "type": "invalid_request_error", "param": null, "code": "invalid_request_error"}}
```

插件会把 `message` 里的 `request_id` 抽出来单独展示，并对状态码给出中文说明：

| 状态码 | 含义 | 是否重试 |
|---|---|---|
| 400 | 请求头/体不合法 | 否 |
| 401 | API Key 无效 | 否 |
| 402 | 余额不足 | 否 |
| 403 | Fetch 被安全策略拒绝 | 否 |
| 404 | Fetch 无法提取正文 | 否 |
| 408 | 读取超时 | 是 |
| 413 | 请求体过大 | 否 |
| 429 | 超过 QPS | 是 |
| 500 / 504 | 服务端或下游错误 | 是 |

重试采用指数退避（1.5^n + 抖动），最多 `max_retries` 次。

## 配置项

| 配置 | 默认 | 说明 |
|---|---|---|
| `api_key` | 空 | 支持多密钥，逗号/换行分隔 |
| `fallback_to_astrbot_key` | true | 复用 AstrBot 已有 DeepSeek 密钥 |
| `base_url` | `https://api.deepseek.com` | API 根地址 |
| `enable_search_tool` | true | 是否注册搜索工具 |
| `enable_fetch_tool` | true | 是否注册抓取工具 |
| `default_count` | 10 | 默认结果条数（1~50） |
| `need_content` | false | 默认是否返回正文 |
| `min_rerank_score` | 0 | 结果质量下限（0~3） |
| `max_results_to_llm` | 8 | 写入模型上下文的结果条数 |
| `summary_max_chars` | 800 | 长摘要截断长度 |
| `content_max_chars` | 2000 | 搜索时正文截断长度 |
| `fetch_max_chars` | 6000 | 抓取正文最大字符数 |
| `timeout_seconds` | 30 | 单次请求超时 |
| `max_retries` | 2 | 重试次数 |
| `enable_llm_hint` | true | 是否注入工具使用提示 |
| `command_reply_style` | detail | 命令回复详细程度 |

## 实现说明

- **参数前置校验**：在发请求前就按官方边界校验 `query`（1~128 字符、非全空白）、
  `count`（1~50）、`url`（≤8192、http/https、含 host、不带账号密码），
  避免把可避免的 400 留给模型。
- **质量过滤**：实测无意义 query 仍会返回 `rerank_score≈0.2` 的噪声条目，
  `min_rerank_score` 可在进入上下文前把这些结果丢掉。
- **失败计费友好**：失败的请求不计费，所以重试 408/429/5xx 不会额外花钱。

## 目录结构

```
astrbot_plugin_deepseek_search/
├── main.py              # 插件入口：命令 + LLM 工具 + 提示词注入
├── deepseek_api.py      # Search / Fetch 客户端、参数校验、错误映射
├── _conf_schema.json    # 插件配置
├── metadata.yaml        # 插件元数据
├── logo.png             # 图标（官方鲸鱼标 + 放大镜）
├── requirements.txt
├── CHANGELOG.md
└── README.md
```

## License

MIT
