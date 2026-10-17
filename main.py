"""DeepSeek 官方搜索接入 —— 把 Search / Fetch 两个官方接口接进 AstrBot。

提供两个 LLM 工具与三条命令：

- ``web_search_deepseek``   检索实时网页，返回带 index 的结构化结果
- ``deepseek_fetch_web_page`` 抓取单个网页并转成 Markdown 正文
- ``/dsearch`` ``/dfetch`` ``/dshelp`` 供人工直接调用

接口规范见 https://api-docs.deepseek.com/zh-cn/guides/search
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .deepseek_api import (
    DEFAULT_BASE_URL,
    DEFAULT_COUNT,
    MAX_COUNT,
    DeepSeekSearchClient,
    DeepSeekSearchError,
    build_query,
    filter_results,
    format_results_for_llm,
    normalize_result,
    validate_query,
)

PLUGIN_NAME = "astrbot_plugin_deepseek_search"

DEFAULT_TEMPLATE = (
    "🔎 **DeepSeek 搜索** · `{query}`\n"
    "共 {count} 条结果（{elapsed:.1f}s）\n"
)


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _strip_command(message: str, names: tuple[str, ...]) -> str:
    """从原始消息里剥掉命令词，取后面的参数。"""
    text = (message or "").strip()
    text = re.sub(r"^[/!！]", "", text)
    for name in sorted(names, key=len, reverse=True):
        if text.lower().startswith(name.lower()):
            rest = text[len(name) :]
            if not rest or rest[0].isspace():
                return rest.strip()
    return text.strip()


class DeepSeekSearchPlugin(Star):
    """DeepSeek 官方搜索接入插件。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.config: dict = config or {}
        self._session: aiohttp.ClientSession | None = None
        self._key_cursor: int = 0

    # ============================================================ 生命周期
    async def initialize(self) -> None:
        logger.info(
            "[DeepSeek 搜索] 插件已加载 | 搜索工具=%s 抓取工具=%s | 密钥来源=%s",
            "开" if self._cfg_bool("enable_search_tool", True) else "关",
            "开" if self._cfg_bool("enable_fetch_tool", True) else "关",
            self._key_source_desc(),
        )

    async def terminate(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    # ============================================================== 配置读取
    def _cfg(self, key: str, default: Any = None) -> Any:
        if isinstance(self.config, dict):
            value = self.config.get(key)
            if value is not None:
                return value
        try:  # AstrBotConfig 兼容
            value = self.config.get(key) if self.config is not None else None
        except Exception:  # noqa: BLE001
            value = None
        return default if value is None else value

    def _cfg_bool(self, key: str, default: bool) -> bool:
        return _as_bool(self._cfg(key), default)

    def _cfg_int(self, key: str, default: int) -> int:
        return _as_int(self._cfg(key), default)

    def _cfg_float(self, key: str, default: float) -> float:
        return _as_float(self._cfg(key), default)

    def _cfg_str(self, key: str, default: str = "") -> str:
        value = self._cfg(key)
        return str(value).strip() if value is not None else default

    # ============================================================== 密钥解析
    def _plugin_keys(self) -> list[str]:
        raw = self._cfg_str("api_key")
        if not raw:
            return []
        return [k.strip() for k in re.split(r"[,\n;]+", raw) if k.strip()]

    def _astrbot_deepseek_keys(self) -> list[str]:
        """从 AstrBot 已有供应商配置里复用 DeepSeek 密钥。"""
        keys: list[str] = []
        try:
            cfg = self._get_context_config()
            if cfg is None:
                return keys
            sources = cfg.get("provider_sources") or []
        except Exception as exc:  # noqa: BLE001
            logger.debug("[DeepSeek 搜索] 读取 AstrBot 供应商配置失败: %s", exc)
            return keys

        if not isinstance(sources, list):
            return keys

        for item in sources:
            if not isinstance(item, dict):
                continue
            provider = str(item.get("provider") or "").lower()
            base_url = str(item.get("api_base") or item.get("base_url") or "").lower()
            if provider != "deepseek" and "api.deepseek.com" not in base_url:
                continue
            for key in item.get("key") or []:
                if isinstance(key, str) and key.strip():
                    keys.append(key.strip())
            single = item.get("api_key")
            if isinstance(single, str) and single.strip():
                keys.append(single.strip())
        # 去重保序
        return list(dict.fromkeys(keys))

    def _all_keys(self) -> list[str]:
        keys = self._plugin_keys()
        if not keys and self._cfg_bool("fallback_to_astrbot_key", True):
            keys = self._astrbot_deepseek_keys()
        return keys

    def _key_source_desc(self) -> str:
        if self._plugin_keys():
            return "插件配置 api_key"
        if self._cfg_bool("fallback_to_astrbot_key", True) and self._astrbot_deepseek_keys():
            return "复用 AstrBot DeepSeek 供应商密钥"
        return "未配置"

    def _next_key(self, keys: list[str]) -> str:
        if not keys:
            return ""
        key = keys[self._key_cursor % len(keys)]
        self._key_cursor = (self._key_cursor + 1) % len(keys)
        return key

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(
                    total=max(5, self._cfg_int("timeout_seconds", 30))
                ),
                trust_env=True,
            )
        return self._session

    async def _client(self) -> DeepSeekSearchClient:
        keys = self._all_keys()
        return DeepSeekSearchClient(
            self._next_key(keys),
            base_url=self._cfg_str("base_url", DEFAULT_BASE_URL) or DEFAULT_BASE_URL,
            timeout=float(max(5, self._cfg_int("timeout_seconds", 30))),
            max_retries=max(0, self._cfg_int("max_retries", 2)),
            session=await self._get_session(),
        )

    # ============================================================ 搜索/抓取封装
    async def _do_search(
        self,
        *,
        query: str,
        count: int | None = None,
        need_content: bool | None = None,
        site: str = "",
        exclude_site: str = "",
        lang: str = "",
        after: str = "",
        before: str = "",
        exact: str = "",
        ori_question: str = "",
    ) -> tuple[list[dict], str, float]:
        """执行一次搜索，返回 (结果列表, 实际 query, 耗时)。"""
        full_query = build_query(
            query,
            site=site,
            exclude_site=exclude_site,
            lang=lang,
            after=after,
            before=before,
            exact=exact,
        )
        validate_query(full_query)

        final_count = (
            count if count is not None else self._cfg_int("default_count", DEFAULT_COUNT)
        )
        final_need_content = (
            need_content
            if need_content is not None
            else self._cfg_bool("need_content", False)
        )
        min_score = self._cfg_float("min_rerank_score", 0.0)

        started = time.perf_counter()
        client = await self._client()
        results = await self._retry_with_failover(
            client,
            lambda c: c.search(
                full_query,
                count=final_count,
                need_content=final_need_content,
                ori_question=ori_question or None,
            ),
        )
        elapsed = time.perf_counter() - started

        results = filter_results(results, min_score)
        ref_id = str(uuid.uuid4())[:4]
        normalized = [normalize_result(item, f"{ref_id}.{i}") for i, item in enumerate(results, 1)]
        return normalized, full_query, elapsed

    async def _retry_with_failover(self, client, call):
        """多密钥时遇到 401/402/429 自动换 key 重试。"""
        keys = self._all_keys()
        last_error: Exception | None = None
        attempts = max(1, len(keys))
        for _ in range(attempts):
            try:
                return await call(client)
            except DeepSeekSearchError as exc:
                last_error = exc
                if len(keys) <= 1 or exc.status not in {401, 402, 429}:
                    raise
                keys_next = self._all_keys()
                client.api_key = self._next_key(keys_next)
                logger.warning(
                    "[DeepSeek 搜索] 当前密钥失败(HTTP %s)，已切换到下一个密钥重试",
                    exc.status,
                )
        raise last_error or DeepSeekSearchError("请求失败。")


    # ================================================================ 命令
    @filter.command("dsearch", alias={"ds", "dssearch"})
    async def cmd_dsearch(self, event: AstrMessageEvent, query: str = ""):
        """DeepSeek 联网搜索。用法：/dsearch 关键词 [--count 5]"""
        raw = (query or "").strip() or _strip_command(
            event.message_str, ("dsearch", "dssearch", "ds")
        )
        if not raw:
            yield event.plain_result(
                "用法：/dsearch 关键词\n"
                "例：/dsearch DeepSeek V4 发布 --count 5\n"
                "更多：/dshelp"
            )
            return

        count, cleaned = self._parse_count_flag(raw)
        try:
            results, full_query, elapsed = await self._do_search(
                query=cleaned or raw,
                count=count,
                ori_question=cleaned or raw,
            )
        except ValueError as exc:
            yield event.plain_result(f"参数不合法：{exc}")
            return
        except DeepSeekSearchError as exc:
            yield event.plain_result(f"搜索失败：{exc.user_message()}")
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("[DeepSeek 搜索] /dsearch 未预期错误")
            yield event.plain_result(f"搜索异常：{type(exc).__name__}: {exc}")
            return

        if not results:
            hint = ""
            if self._cfg_float("min_rerank_score", 0.0) > 0:
                hint = "\n（可尝试在配置里调低 min_rerank_score）"
            yield event.plain_result(f"没有找到「{full_query}」的相关结果。{hint}")
            return

        yield event.plain_result(self._render_chat(results, full_query, elapsed))

    @filter.command("dfetch", alias={"dsf", "dsfetch"})
    async def cmd_dfetch(self, event: AstrMessageEvent, url: str = ""):
        """抓取网页正文并转成 Markdown。用法：/dfetch https://..."""
        target = (url or "").strip() or _strip_command(
            event.message_str, ("dfetch", "dsfetch", "dsf")
        )
        if not target:
            yield event.plain_result(
                "用法：/dfetch https://example.com/article\n"
                "DeepSeek Fetch 会把网页转成 Markdown 正文返回。"
            )
            return

        try:
            page = await self._fetch_page(target)
        except ValueError as exc:
            yield event.plain_result(f"参数不合法：{exc}")
            return
        except DeepSeekSearchError as exc:
            yield event.plain_result(f"抓取失败：{exc.user_message()}")
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("[DeepSeek 搜索] /dfetch 未预期错误")
            yield event.plain_result(f"抓取异常：{type(exc).__name__}: {exc}")
            return

        limit = max(200, self._cfg_int("fetch_max_chars", 6000))
        content = page["content"]
        clipped = content[:limit]
        suffix = f"\n\n…（已截断，原文共 {len(content)} 字符）" if len(content) > limit else ""
        yield event.plain_result(
            f"📄 **{page['title'] or '(无标题)'}**\n{page['url']}\n\n{clipped}{suffix}"
        )

    @filter.command("dshelp")
    async def cmd_dshelp(self, event: AstrMessageEvent):
        """查看 DeepSeek 官方搜索接入的用法。"""
        source = self._key_source_desc()
        yield event.plain_result(
            "**DeepSeek 官方搜索接入**\n"
            "· /dsearch 关键词 [--count 1~50] — 联网搜索\n"
            "· /dfetch https://… — 抓取网页转 Markdown\n"
            "· /dshelp — 本说明\n\n"
            "**搜索语法**（直接写在关键词里）\n"
            "· site:github.com 只搜指定站；-site:xxx.com 排除\n"
            '· "精确短语"；-排除词；lang:zh,en；after:2025；before:2026\n\n'
            f"**计费**：Search / Fetch 各 5 元 / 1000 次成功请求，"
            f"失败或空结果不计费；账号级各 5 QPS。\n"
            f"**密钥来源**：{source}"
        )

    # ================================================================ LLM 工具
    @filter.llm_tool(name="web_search_deepseek")
    async def tool_web_search(
        self,
        event: AstrMessageEvent,
        query: str,
        count: int = 10,
        need_content: bool = False,
        site: str = "",
        exclude_site: str = "",
        lang: str = "",
        after: str = "",
        before: str = "",
        exact: str = "",
    ) -> str:
        """用 DeepSeek 官方 Search 接口检索实时网页，返回带标题、链接、摘要与权威度的结构化结果。

        适用于：查询新闻与时效性信息、核实事实、需要给出可点击来源链接的场景。
        返回结果的 index 可作引用标记。若结果不足以回答，可再用 deepseek_fetch_web_page 抓取原文细读。

        Args:
            query(string): 搜索词，1~128 个字符。支持 site:、-site:、lang:、"精确短语"、after:、before: 运算符。
            count(number): 返回结果条数，1~50，默认 10。条数不影响计费。
            need_content(boolean): 是否同时返回网页正文，默认 false。开启会显著增大返回体积。
            site(string): 只在这些站点内搜索，多个用逗号分隔，例如 github.com,huggingface.co。
            exclude_site(string): 排除这些站点，多个用逗号分隔。
            lang(string): 限定结果语言，例如 zh 或 en 或 zh,en。
            after(string): 只保留此日期之后发布的网页，例如 2025 或 2025-01-01。
            before(string): 只保留此日期之前发布的网页，例如 2026 或 2026-06-01。
            exact(string): 需要精确匹配的短语。
        """
        if not self._cfg_bool("enable_search_tool", True):
            return "DeepSeek 搜索工具已在插件配置中关闭（enable_search_tool=false）。"

        try:
            results, full_query, elapsed = await self._do_search(
                query=query,
                count=_as_int(count, self._cfg_int("default_count", DEFAULT_COUNT)),
                need_content=_as_bool(need_content, False),
                site=site,
                exclude_site=exclude_site,
                lang=lang,
                after=after,
                before=before,
                exact=exact,
                ori_question=query,
            )
        except ValueError as exc:
            return f"参数不合法：{exc}"
        except DeepSeekSearchError as exc:
            return f"搜索失败：{exc.user_message()}"
        except Exception as exc:  # noqa: BLE001
            logger.exception("[DeepSeek 搜索] 工具调用未预期错误")
            return f"搜索异常：{type(exc).__name__}: {exc}"

        if not results:
            return (
                f"DeepSeek 搜索「{full_query}」没有返回结果。"
                "可以换更宽泛的关键词重试，或去掉 site/lang/after 等限定条件。"
            )

        logger.info(
            "[DeepSeek 搜索] query=%r count=%s 命中=%s 耗时=%.2fs",
            full_query,
            len(results),
            len(results),
            elapsed,
        )
        return format_results_for_llm(
            results,
            query=full_query,
            ref_id=results[0]["index"].split(".")[0],
            max_results=max(1, self._cfg_int("max_results_to_llm", 8)),
            summary_max_chars=max(200, self._cfg_int("summary_max_chars", 800)),
            include_content=_as_bool(need_content, False),
            content_max_chars=max(500, self._cfg_int("content_max_chars", 2000)),
        )

    @filter.llm_tool(name="deepseek_fetch_web_page")
    async def tool_fetch_page(
        self,
        event: AstrMessageEvent,
        url: str,
        max_chars: int = 6000,
    ) -> str:
        """用 DeepSeek 官方 Fetch 接口抓取单个网页，返回标题和 Markdown 正文。

        适用于：需要阅读搜索结果之外的具体页面、核对原文细节、提取长文内容。
        对同一域名请避免高频调用（官方对目标域名有独立 QPS 限制）。

        Args:
            url(string): 要抓取的网页地址，必须是含主机名的完整 http 或 https URL。
            max_chars(number): 正文最多返回的字符数，默认 6000。
        """
        if not self._cfg_bool("enable_fetch_tool", True):
            return "DeepSeek 网页抓取工具已在插件配置中关闭（enable_fetch_tool=false）。"

        try:
            page = await self._fetch_page(_strip_command(str(url or ""), ("dfetch",)))
        except ValueError as exc:
            return f"参数不合法：{exc}"
        except DeepSeekSearchError as exc:
            return f"抓取失败：{exc.user_message()}"
        except Exception as exc:  # noqa: BLE001
            logger.exception("[DeepSeek 搜索] 抓取工具未预期错误")
            return f"抓取异常：{type(exc).__name__}: {exc}"

        limit = max(
            200, min(_as_int(max_chars, 6000), self._cfg_int("fetch_max_chars", 6000))
        )
        content = page["content"][:limit]
        note = (
            f"\n\n[正文已截断，原文共 {len(page['content'])} 字符]"
            if len(page["content"]) > limit
            else ""
        )
        if not content.strip():
            return f"Fetch 成功但未取到正文：{page['url']}"
        return f"Title: {page['title'] or '(无标题)'}\nURL: {page['url']}\n\n{content}{note}"

    async def _fetch_page(self, url: str) -> dict:
        client = await self._client()
        return await self._retry_with_failover(client, lambda c: c.fetch(url))

    # ============================================================ 提示词注入
    @filter.on_llm_request()
    async def inject_tool_hint(self, event: AstrMessageEvent, req) -> None:
        """提示模型优先使用本插件的搜索/抓取工具。"""
        if not self._cfg_bool("enable_llm_hint", True):
            return
        tools = []
        if self._cfg_bool("enable_search_tool", True):
            tools.append("web_search_deepseek")
        if self._cfg_bool("enable_fetch_tool", True):
            tools.append("deepseek_fetch_web_page")
        if not tools:
            return

        hint = (
            "\n[DeepSeek 联网工具]\n"
            f"可用工具：{', '.join(tools)}。\n"
            "当问题涉及实时信息、新闻、价格、版本发布等可能超出训练数据的内容时，"
            "先用 web_search_deepseek 检索；需要核对原文细节时再用 "
            "deepseek_fetch_web_page 抓取页面。引用来源请使用结果里的 url。\n"
        )
        try:
            system_prompt = getattr(req, "system_prompt", "") or ""
            if "web_search_deepseek" not in system_prompt:
                req.system_prompt = system_prompt + hint
        except Exception:  # noqa: BLE001
            pass

    # ================================================================ 渲染
    def _render_chat(self, results: list[dict], query: str, elapsed: float) -> str:
        style = self._cfg_str("command_reply_style", "detail")
        lines = [
            DEFAULT_TEMPLATE.format(
                query=query, count=len(results), elapsed=elapsed
            ).strip()
        ]
        limit = max(1, self._cfg_int("max_results_to_llm", 8))
        for i, item in enumerate(results[:limit], 1):
            title = item.get("title") or item.get("url") or "(无标题)"
            lines.append(f"\n**{i}. {title}**")
            lines.append(item.get("url") or "")
            if style == "detail":
                body = item.get("snippet") or item.get("summary") or ""
                if body:
                    lines.append(body[:160] + ("…" if len(body) > 160 else ""))
                meta = []
                if item.get("site_name"):
                    meta.append(str(item["site_name"]))
                if item.get("published"):
                    meta.append(str(item["published"])[:10])
                if item.get("authority") not in (None, -1):
                    meta.append(f"权威度 {item['authority']}/4")
                if item.get("score") is not None:
                    meta.append(f"相关度 {item['score']}")
                if meta:
                    lines.append("· " + " · ".join(meta))
        return "\n".join(lines)

    @staticmethod
    def _parse_count_flag(raw: str) -> tuple[int | None, str]:
        """支持 /dsearch 关键词 --count 5 的写法。"""
        count = None
        text = raw
        matched = re.search(r"(?:^|\s)--?count[=\s]+(\d{1,3})", text)
        if matched:
            count = max(1, min(MAX_COUNT, int(matched.group(1))))
            text = (text[: matched.start()] + text[matched.end() :]).strip()
        return count, text
