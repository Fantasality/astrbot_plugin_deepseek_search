# Changelog

## 1.0.0

首个版本。

- 接入 DeepSeek 官方 `POST /v1/tools/search` 与 `POST /v1/tools/fetch`。
- LLM 工具：`web_search_deepseek`、`deepseek_fetch_web_page`。
- 命令：`/dsearch`、`/dfetch`、`/dshelp`（含 `/ds`、`/dsf` 等别名）。
- 支持官方搜索运算符，并提供 `site` / `exclude_site` / `lang` / `after` / `before` / `exact` 参数自动拼接。
- 发请求前按官方边界校验 `query` / `count` / `url`。
- 多密钥轮询与 401/402/429 故障切换；支持复用 AstrBot 已配置的 DeepSeek 密钥。
- 完整错误映射（400/401/402/403/404/408/413/429/500/504）与 408/429/5xx 指数退避重试。
- `min_rerank_score` 质量过滤，剔除低相关噪声结果。
