"""DeepSeek 官方 Search / Fetch 接口客户端。

严格按官方规范实现（https://api-docs.deepseek.com/zh-cn/guides/search）：

- Search: ``POST https://api.deepseek.com/v1/tools/search``
- Fetch:  ``POST https://api.deepseek.com/v1/tools/fetch``

两者都使用 Bearer 鉴权、JSON 请求体，按「成功请求次数」计费
（各 5 元 / 1000 次，仅 HTTP 200 且产生结果时计费）。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from typing import Any

import aiohttp

DEFAULT_BASE_URL = "https://api.deepseek.com"
SEARCH_PATH = "/v1/tools/search"
FETCH_PATH = "/v1/tools/fetch"

# ---- 官方字段边界（见 Search / Fetch API Reference）----
MAX_QUERY_CHARS = 128
MIN_COUNT = 1
MAX_COUNT = 50
DEFAULT_COUNT = 10
MAX_URL_CHARS = 8192
MIN_AUTHORITY = -1
MAX_AUTHORITY = 4

# 官方「错误」章节列出的 HTTP 状态码说明
STATUS_HINTS: dict[int, str] = {
    400: "请求头或请求体不合法（JSON 格式错误或字段值无效）",
    401: "API Key 缺失或无效",
    402: "账号余额不足，请先充值",
    403: "Fetch 请求被安全策略拒绝",
    404: "Fetch 未能获取可用的网页内容（URL 不可用、被站点策略拦截、"
    "目标站点返回错误、内容类型不受支持或页面无可提取正文）",
    408: "读取请求体超时",
    413: "请求体超过大小限制",
    429: "超过 QPS 限制（账号级 Search/Fetch 各 5 QPS，Fetch 另有目标域名 QPS 限制）",
    500: "DeepSeek 服务端内部错误",
    504: "下游请求超时",
}
# 官方建议：408 / 429 / 5xx 等待后重试，其它 4xx 不要重试
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
NOT_RETRYABLE_HINT = "该错误无需重试，不修改请求不会成功。"


class DeepSeekSearchError(Exception):
    """Search / Fetch 调用失败。"""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        err_type: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.err_type = err_type
        self.request_id = request_id

    def user_message(self) -> str:
        """给用户/模型看的单行说明。"""
        parts: list[str] = []
        if self.status is not None:
            parts.append(f"HTTP {self.status}")
        hint = STATUS_HINTS.get(self.status or 0)
        if hint:
            parts.append(hint)
        parts.append(self.message)
        if self.request_id:
            parts.append(f"request_id={self.request_id}")
        return " · ".join(p for p in parts if p)

    def retryable(self) -> bool:
        if self.status is None:
            # 网络层异常（DNS / 连接 / 超时）通常可重试
            return True
        return self.status in RETRYABLE_STATUS


# ----------------------------------------------------------------- 参数校验
def validate_query(query: str) -> str:
    """校验 query：1~128 个 Unicode 字符，且不能全为空白。"""
    text = str(query or "")
    if not text.strip():
        raise ValueError("query 不能为空白，需包含 1~128 个 Unicode 字符。")
    if len(text) > MAX_QUERY_CHARS:
        raise ValueError(
            f"query 超长：{len(text)} 字符，官方上限 {MAX_QUERY_CHARS} 字符。"
            "请精简关键词，或把限定条件放进 site/lang/after/before 参数。"
        )
    return text


def normalize_count(count: Any) -> int:
    """把 count 收敛到官方的 1~50。"""
    try:
        value = int(count)
    except (TypeError, ValueError):
        return DEFAULT_COUNT
    return max(MIN_COUNT, min(MAX_COUNT, value))


def validate_url(url: str) -> str:
    """校验 url：必须是含 host 的 http/https URL，且不能带用户名密码。"""
    text = str(url or "").strip()
    if not text:
        raise ValueError("url 不能为空。")
    if len(text) > MAX_URL_CHARS:
        raise ValueError(f"url 超长：{len(text)} 字符，官方上限 {MAX_URL_CHARS}。")
    if not re.match(r"^https?://", text, re.IGNORECASE):
        raise ValueError("url 必须是 http:// 或 https:// 开头的完整地址。")
    rest = re.sub(r"^https?://", "", text, flags=re.IGNORECASE)
    host = rest.split("/")[0].split("?")[0].split("#")[0]
    if "@" in host:
        raise ValueError("url 不能携带用户名或密码（例如 https://user:pass@host/）。")
    if not host or host.startswith(":"):
        raise ValueError("url 缺少主机名（host）。")
    return text


def _split_terms(value: Any) -> list[str]:
    if not value:
        return []
    return [p.strip() for p in re.split(r"[,\s]+", str(value)) if p.strip()]


def build_query(
    query: str,
    *,
    site: str = "",
    exclude_site: str = "",
    lang: str = "",
    after: str = "",
    before: str = "",
    exact: str = "",
) -> str:
    """把结构化筛选条件拼成官方支持的 query 运算符。

    官方语法：site: / -site: / -词 / -"短语" / "短语" / lang: / after: / before:
    """
    parts = [str(query or "").strip()]

    sites = _split_terms(site)
    if sites:
        parts.append(" OR ".join(f"site:{s}" for s in sites))

    for s in _split_terms(exclude_site):
        parts.append(f"-site:{s}")

    langs = [p.strip() for p in str(lang or "").split(",") if p.strip()]
    if langs:
        parts.append(f"lang:{','.join(langs)}")

    if str(after or "").strip():
        parts.append(f"after:{str(after).strip()}")
    if str(before or "").strip():
        parts.append(f"before:{str(before).strip()}")

    if str(exact or "").strip():
        parts.append(f'"{str(exact).strip()}"')

    return " ".join(p for p in parts if p)


def filter_results(
    results: list[dict], min_rerank_score: float = 0.0
) -> list[dict]:
    """按 rerank_score 过滤低质量结果。

    官方说明 rerank_score 0~3，≥1.5 为优质结果。实测无意义查询也会返回
    0.19 左右的噪声条目，故允许设定下限把这类结果丢掉。
    """
    if not results:
        return []
    if min_rerank_score <= 0:
        return list(results)
    kept = []
    for item in results:
        try:
            score = float(item.get("rerank_score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        if score >= min_rerank_score:
            kept.append(item)
    return kept


def normalize_result(item: dict, index: str) -> dict:
    """把官方返回项整理成给模型/前端用的稳定结构。"""
    date = item.get("date")
    return {
        "index": index,
        "title": (item.get("title") or "").strip(),
        "url": (item.get("url") or "").strip(),
        "snippet": (item.get("snippet") or "").strip(),
        "summary": (item.get("summary") or "").strip(),
        "content": item.get("content") or "",
        "site_name": item.get("site_name"),
        "site_icon": item.get("site_icon"),
        "published": date,
        "authority": item.get("authority"),
        "score": round(float(item.get("rerank_score") or 0), 3),
    }


def format_results_for_llm(
    results: list[dict],
    *,
    query: str,
    ref_id: str,
    max_results: int = 8,
    summary_max_chars: int = 800,
    include_content: bool = False,
    content_max_chars: int = 2000,
) -> str:
    """输出给 LLM 的 JSON 文本（带可引用 index）。"""
    rows = []
    for i, item in enumerate(results[: max(1, max_results)], 1):
        row = {
            "index": f"{ref_id}.{i}",
            "title": item.get("title") or item.get("url") or "(无标题)",
            "url": item.get("url"),
            "snippet": item.get("snippet"),
            "summary": (item.get("summary") or "")[:summary_max_chars],
            "site": item.get("site_name"),
            "published": item.get("published"),
            "authority": item.get("authority"),
            "score": item.get("score"),
        }
        content = item.get("content") or ""
        if include_content and content:
            row["content"] = content[:content_max_chars]
        rows.append(row)

    payload = {
        "provider": "deepseek_search",
        "query": query,
        "returned": len(rows),
        "results": rows,
        "citation": (
            "回答时如需给出处，请使用 results 里的 url，并在正文中以 [index] 形式标注。"
        ),
    }
    return json.dumps(payload, ensure_ascii=False)


# ----------------------------------------------------------------- 客户端
class DeepSeekSearchClient:
    """Search / Fetch 的轻量异步客户端。"""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 2,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = max(5.0, float(timeout))
        self.max_retries = max(0, int(max_retries))
        self._session = session
        self._own_session = session is None

    # -- 生命周期 -----------------------------------------------------
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                trust_env=True,  # 跟随系统代理，和 AstrBot 内置搜索工具一致
            )
            self._own_session = True
        return self._session

    async def close(self) -> None:
        if self._own_session and self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    # -- 通用请求 -----------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            # 与 AstrBot 内置搜索工具一致，规避 aiohttp 的 brotli 解压兼容问题
            "Accept-Encoding": "gzip, deflate",
        }

    async def _post(self, path: str, payload: dict) -> dict:
        if not self.api_key:
            raise DeepSeekSearchError(
                "未配置 DeepSeek API Key。请在插件配置中填写 api_key，"
                "或开启 fallback_to_astrbot_key 复用 AstrBot 里已有的 DeepSeek 供应商密钥。"
            )

        url = f"{self.base_url}{path}"
        session = await self._get_session()
        last_error: DeepSeekSearchError | None = None

        for attempt in range(self.max_retries + 1):
            try:
                async with session.post(
                    url, json=payload, headers=self._headers()
                ) as resp:
                    body = await resp.text()
                    if resp.status == 200:
                        try:
                            data = json.loads(body)
                        except json.JSONDecodeError as exc:
                            raise DeepSeekSearchError(
                                f"响应不是合法 JSON：{exc}",
                                status=resp.status,
                            ) from exc
                        if not isinstance(data, dict):
                            raise DeepSeekSearchError(
                                "响应结构异常：顶层不是对象。", status=resp.status
                            )
                        return data

                    message, code, err_type, request_id = _parse_error(body)
                    error = DeepSeekSearchError(
                        message,
                        status=resp.status,
                        code=code,
                        err_type=err_type,
                        request_id=request_id,
                    )
                    if error.retryable() and attempt < self.max_retries:
                        last_error = error
                        await asyncio.sleep(_backoff(attempt))
                        continue
                    raise error

            except DeepSeekSearchError:
                raise
            except asyncio.TimeoutError as exc:
                error = DeepSeekSearchError(
                    f"请求超时（{self.timeout:.0f}s）。官方建议稍后重试。"
                )
                if attempt < self.max_retries:
                    last_error = error
                    await asyncio.sleep(_backoff(attempt))
                    continue
                raise error from exc
            except aiohttp.ClientError as exc:
                error = DeepSeekSearchError(f"网络请求失败：{exc}")
                if attempt < self.max_retries:
                    last_error = error
                    await asyncio.sleep(_backoff(attempt))
                    continue
                raise error from exc

        raise last_error or DeepSeekSearchError("请求失败（未知原因）。")

    # -- Search -------------------------------------------------------
    async def search(
        self,
        query: str,
        *,
        count: int = DEFAULT_COUNT,
        need_content: bool = False,
        ori_question: str | None = None,
        localization: str = "",
    ) -> list[dict]:
        """调用 Search 接口，返回官方 results 列表。"""
        payload: dict[str, Any] = {
            "query": validate_query(query),
            "count": normalize_count(count),
            "need_content": bool(need_content),
        }
        if ori_question:
            text = str(ori_question).strip()
            if text:
                payload["ori_question"] = text[:MAX_QUERY_CHARS]
        if localization:
            payload["localization"] = str(localization).strip()

        data = await self._post(SEARCH_PATH, payload)
        results = data.get("results")
        if results is None:
            raise DeepSeekSearchError("响应缺少 results 字段。")
        if not isinstance(results, list):
            raise DeepSeekSearchError("响应 results 字段不是数组。")
        return [r for r in results if isinstance(r, dict)]

    # -- Fetch --------------------------------------------------------
    async def fetch(self, url: str) -> dict:
        """调用 Fetch 接口，返回 {title, content}。"""
        data = await self._post(FETCH_PATH, {"url": validate_url(url)})
        return {
            "title": (data.get("title") or "").strip(),
            "content": data.get("content") or "",
            "url": url,
        }


def _backoff(attempt: int) -> float:
    return min(8.0, (1.5**attempt) + random.uniform(0, 0.4))


def _parse_error(body: str) -> tuple[str, str | None, str | None, str | None]:
    """解析官方 OpenAI 兼容错误体：{"error": {message, type, param, code}}。"""
    message, code, err_type, request_id = "", None, None, None
    try:
        data = json.loads(body)
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            message = str(err.get("message") or "")
            code = err.get("code")
            err_type = err.get("type")
    except (json.JSONDecodeError, AttributeError):
        message = (body or "").strip()[:500]

    if not message:
        message = (body or "").strip()[:300] or "未知错误"

    matched = re.search(r"request_id:\s*([0-9a-fA-F-]{8,})", message)
    if matched:
        request_id = matched.group(1)
        message = re.sub(r"\s*\(request_id:[^)]*\)", "", message).strip()

    return message, code, err_type, request_id
