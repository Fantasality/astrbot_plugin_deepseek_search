"""DeepSeek 官方搜索接入 · 冒烟测试。

覆盖四层：
  1. 静态校验   元数据 / 配置 schema / 目录结构 / 编译
  2. 单元逻辑   参数边界 / 运算符拼接 / 错误解析 / 结果过滤
  3. 真实接口   真网调用 Search 与 Fetch（需要 key）
  4. 插件集成   真实实例化插件，走工具方法与命令，校验返回结构

运行：实例 venv
  <instance>/venv/Scripts/python.exe tests/test_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT_F = Path(ROOT)
WORKSPACE = os.path.dirname(ROOT)
INSTANCE = r"C:\Users\Admin\.astrbot_launcher\instances\4eaca999-aab2-49ff-b9ca-f789cfa948c0"
CORE = os.path.join(INSTANCE, "core")

sys.path.insert(0, CORE)
sys.path.insert(0, WORKSPACE)

import yaml  # noqa: E402

from astrbot_plugin_deepseek_search import deepseek_api as API  # noqa: E402
from astrbot_plugin_deepseek_search.main import (  # noqa: E402
    DeepSeekSearchPlugin,
    _strip_command,
)

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(name)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def load_real_config() -> dict:
    path = os.path.join(CORE, "data", "cmd_config.json")
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


class FakeContext:
    """最小可用的 Context 替身，只为 get_config 服务。"""

    def __init__(self, config: dict) -> None:
        self._config = config

    def get_config(self, *args, **kwargs):
        return self._config


class FakeEvent:
    """命令测试用的最小事件替身。"""

    def __init__(self, message: str = "") -> None:
        self.message_str = message
        self.unified_msg_origin = "test:group:1"

    def plain_result(self, text: str):
        return ("plain", text)

    def stop_event(self) -> None:
        pass


# ===================================================== 1. 静态校验
print("=" * 68)
print("1. 静态校验")
print("=" * 68)

meta = yaml.safe_load((ROOT_F / "metadata.yaml").read_text(encoding="utf-8"))
REQUIRED = ("name", "desc", "version", "author")
missing = [f for f in REQUIRED if f not in meta]
check("metadata 必填字段齐全", not missing, str(missing))
invalid = [f for f in REQUIRED if not isinstance(meta.get(f), str) or not meta[f].strip()]
check("metadata 必填字段为非空字符串", not invalid, str(invalid))
check("display_name 为中文名", meta.get("display_name") == "DeepSeek 官方搜索接入",
      str(meta.get("display_name")))
check("short_desc 存在", isinstance(meta.get("short_desc"), str) and bool(meta["short_desc"]))
check("repo 指向 GitHub", str(meta.get("repo", "")).startswith("https://github.com/"))
check("astrbot_version 为 PEP440 区间", str(meta.get("astrbot_version", "")).startswith(">="),
      str(meta.get("astrbot_version")))
check("tags 为非空列表", isinstance(meta.get("tags"), list) and len(meta["tags"]) > 0)

# 用 AstrBot 官方校验函数复核
from astrbot.core.star.updater import _PluginUpdater  # noqa: E402

try:
    _PluginUpdater.validate_plugin_metadata(meta, "metadata.yaml")
    check("AstrBot 官方元数据校验通过", True)
except Exception as exc:  # noqa: BLE001
    check("AstrBot 官方元数据校验通过", False, str(exc))

for rel in ("main.py", "deepseek_api.py", "metadata.yaml", "_conf_schema.json",
            "logo.png", "README.md", "requirements.txt", "CHANGELOG.md"):
    check(f"存在 {rel}", os.path.isfile(os.path.join(ROOT, rel)))

logo_path = os.path.join(ROOT, "logo.png")
try:
    from PIL import Image

    with Image.open(logo_path) as im:
        check("logo.png 为正方形", im.width == im.height, f"{im.width}x{im.height}")
        check("logo.png 边长 ≥256", im.width >= 256, str(im.width))
        check("logo.png 含透明通道", im.mode in ("RGBA", "LA", "P"), im.mode)
except Exception as exc:  # noqa: BLE001
    check("logo.png 可被 PIL 读取", False, str(exc))

schema = json.loads((ROOT_F / "_conf_schema.json").read_text(encoding="utf-8"))
schema_ok = all(
    {"type", "description", "default"} <= set(v) for v in schema.values()
)
check("配置 schema 字段完整(type/description/default)", schema_ok, f"{len(schema)} 项")
check("配置项 api_key 存在", "api_key" in schema)
check("配置项 enable_search_tool / enable_fetch_tool 存在",
      {"enable_search_tool", "enable_fetch_tool"} <= set(schema))


# ===================================================== 2. 单元逻辑
print()
print("=" * 68)
print("2. 单元逻辑（参数边界 / 语法拼接 / 错误解析）")
print("=" * 68)

# query 边界
try:
    API.validate_query("正常查询")
    check("query 正常值通过", True)
except ValueError as exc:
    check("query 正常值通过", False, str(exc))

for bad, label in [("", "空字符串"), ("   ", "纯空白"), ("x" * 129, "129 字符超长")]:
    try:
        API.validate_query(bad)
        check(f"query 拒绝{label}", False, "未抛异常")
    except ValueError:
        check(f"query 拒绝{label}", True)

try:
    API.validate_query("x" * 128)
    check("query 恰好 128 字符通过", True)
except ValueError as exc:
    check("query 恰好 128 字符通过", False, str(exc))

# count 收敛
check("count 0 → 1", API.normalize_count(0) == 1, str(API.normalize_count(0)))
check("count 99 → 50", API.normalize_count(99) == 50, str(API.normalize_count(99)))
check("count 非法值 → 默认 10", API.normalize_count("abc") == 10)
check("count 字符串 '7' → 7", API.normalize_count("7") == 7)

# url 边界
for good in ("https://example.com", "http://a.cn/path?q=1"):
    try:
        API.validate_url(good)
        check(f"url 接受 {good}", True)
    except ValueError as exc:
        check(f"url 接受 {good}", False, str(exc))

for bad, label in [
    ("", "空"),
    ("not-a-url", "无协议"),
    ("ftp://example.com", "非 http"),
    ("https://", "缺 host"),
    ("https://user:pass@example.com", "带账号密码"),
    ("https://a.com/" + "x" * 8200, "超 8192"),
]:
    try:
        API.validate_url(bad + ("" if label != "超 8192" else ""))
        check(f"url 拒绝{label}", False, "未抛异常")
    except ValueError:
        check(f"url 拒绝{label}", True)

# 运算符拼接
q = API.build_query("DeepSeek", site="github.com,huggingface.co",
                    exclude_site="example.com", lang="zh,en",
                    after="2025-01-01", before="2026-06-01", exact="精确短语")
check("site 多站拼成 OR", "site:github.com OR site:huggingface.co" in q, q)
check("排除站点用 -site:", "-site:example.com" in q)
check("lang 用逗号连接", "lang:zh,en" in q)
check("after / before 拼入", "after:2025-01-01" in q and "before:2026-06-01" in q)
check("精确短语加引号", '"精确短语"' in q)

check("空筛选条件不产生多余空格", API.build_query(" 测试 ") == "测试",
      repr(API.build_query(" 测试 ")))

# 错误解析
msg, code, etype, rid = API._parse_error(
    '{"error":{"message":"bad (request_id: 9575d066-6951-45e3-bfe6-fce5e9566336)",'
    '"type":"invalid_request_error","param":null,"code":"invalid_request_error"}}'
)
check("错误体解析 message", "bad" == msg, msg)
check("错误体抽取 request_id", rid == "9575d066-6951-45e3-bfe6-fce5e9566336", str(rid))
check("错误体解析 code", code == "invalid_request_error", str(code))

msg2, *_ = API._parse_error("纯文本错误")
check("非 JSON 错误体兜底", msg2 == "纯文本错误", msg2)

# 可重试判定
check("408 可重试", API.DeepSeekSearchError("x", status=408).retryable())
check("429 可重试", API.DeepSeekSearchError("x", status=429).retryable())
check("500 可重试", API.DeepSeekSearchError("x", status=500).retryable())
check("400 不可重试", not API.DeepSeekSearchError("x", status=400).retryable())
check("401 不可重试", not API.DeepSeekSearchError("x", status=401).retryable())
check("402 不可重试", not API.DeepSeekSearchError("x", status=402).retryable())

# 质量过滤
sample = [
    {"rerank_score": 2.4, "title": "A"},
    {"rerank_score": 1.5, "title": "B"},
    {"rerank_score": 0.19, "title": "C"},
]
check("min_score=0 保留全部", len(API.filter_results(sample, 0)) == 3)
check("min_score=1.5 只留优质", len(API.filter_results(sample, 1.5)) == 2)
check("min_score=2.0 只留 1 条", len(API.filter_results(sample, 2.0)) == 1)

# 命令参数剥离
check("剥离 /dsearch 前缀", _strip_command("/dsearch 天气", ("dsearch", "ds")) == "天气")
check("剥离 ds 前缀", _strip_command("/ds 天气", ("dsearch", "ds")) == "天气")
check("无前缀原样返回", _strip_command("天气", ("dsearch",)) == "天气")
check("不误伤前缀相似的词", _strip_command("/dssearchx", ("dsearch", "ds")) == "dssearchx")


# ===================================================== 3+4. 真实接口与插件集成
async def integration() -> None:
    print()
    print("=" * 68)
    print("3. 真实接口调用")
    print("=" * 68)

    cfg = load_real_config()
    plugin_cfg = {
        "api_key": "",
        "fallback_to_astrbot_key": True,
        "base_url": "https://api.deepseek.com",
        "enable_search_tool": True,
        "enable_fetch_tool": True,
        "default_count": 5,
        "need_content": False,
        "min_rerank_score": 0.0,
        "max_results_to_llm": 8,
        "summary_max_chars": 800,
        "content_max_chars": 2000,
        "fetch_max_chars": 6000,
        "timeout_seconds": 30,
        "max_retries": 2,
        "enable_llm_hint": True,
        "command_reply_style": "detail",
    }

    plugin = DeepSeekSearchPlugin(FakeContext(cfg), plugin_cfg)
    await plugin.initialize()

    keys = plugin._all_keys()
    check("密钥解析（复用 AstrBot DeepSeek 供应商）", len(keys) >= 1,
          f"来源={plugin._key_source_desc()}，数量={len(keys)}")
    if not keys:
        print("!! 无可用密钥，跳过真实接口测试")
        return

    # --- 客户端直连 ---
    client = API.DeepSeekSearchClient(keys[0], timeout=30, max_retries=1)
    try:
        started = time.perf_counter()
        results = await client.search("DeepSeek V4 模型发布", count=3)
        elapsed = time.perf_counter() - started
        check("Search 接口返回结果", len(results) > 0, f"{len(results)} 条 / {elapsed:.2f}s")
        if results:
            first = results[0]
            fields = {"rerank_score", "title", "url", "snippet", "summary",
                      "content", "site_name", "site_icon", "date", "authority"}
            check("返回项字段符合官方 schema", fields <= set(first),
                  f"缺 {fields - set(first)}")
            check("rerank_score 在 0~3", 0 <= float(first["rerank_score"]) <= 3,
                  str(first["rerank_score"]))
            check("authority 在 -1~4", -1 <= int(first["authority"]) <= 4,
                  str(first["authority"]))
            check("url 为 http(s)", str(first["url"]).startswith("http"))
            check("content 在 need_content=false 时为空",
                  first["content"] == "", repr(first["content"])[:40])
    except Exception as exc:  # noqa: BLE001
        check("Search 接口返回结果", False, f"{type(exc).__name__}: {exc}")

    try:
        page = await client.fetch("https://api-docs.deepseek.com/zh-cn/guides/search")
        check("Fetch 接口返回 title/content",
              bool(page["title"]) and len(page["content"]) > 100,
              f"title={page['title']!r} content={len(page['content'])} 字符")
        check("Fetch 正文为 Markdown 风格", "#" in page["content"] or "Search" in page["content"])
    except Exception as exc:  # noqa: BLE001
        check("Fetch 接口返回 title/content", False, f"{type(exc).__name__}: {exc}")

    # --- need_content=true ---
    try:
        rich = await client.search("苏州大学 宿舍", count=1, need_content=True)
        has_content = bool(rich and rich[0].get("content"))
        check("need_content=true 返回正文", has_content,
              f"{len(rich[0]['content']) if rich else 0} 字符")
    except Exception as exc:  # noqa: BLE001
        check("need_content=true 返回正文", False, f"{type(exc).__name__}: {exc}")

    # --- 错误路径：非法 key ---
    bad_client = API.DeepSeekSearchClient("sk-definitely-invalid", timeout=20, max_retries=0)
    try:
        await bad_client.search("test")
        check("无效密钥返回 401", False, "未抛异常")
    except API.DeepSeekSearchError as exc:
        check("无效密钥返回 401", exc.status == 401, exc.user_message()[:80])
        check("401 不可重试", not exc.retryable())
    except Exception as exc:  # noqa: BLE001
        check("无效密钥返回 401", False, f"{type(exc).__name__}: {exc}")

    # --- 错误路径：非法 url（客户端前置拦截）---
    try:
        await client.fetch("not-a-url")
        check("非法 url 前置拦截", False, "未抛异常")
    except ValueError:
        check("非法 url 前置拦截", True)
    except Exception as exc:  # noqa: BLE001
        check("非法 url 前置拦截", False, f"{type(exc).__name__}")

    await client.close()
    await bad_client.close()

    print()
    print("=" * 68)
    print("4. 插件集成（工具方法 / 命令 / 提示词注入）")
    print("=" * 68)

    event = FakeEvent("测试")

    # 工具 1：搜索
    raw = await plugin.tool_web_search(event, query="DeepSeek 官方搜索 API", count=3)
    try:
        payload = json.loads(raw)
        ok_json = True
    except json.JSONDecodeError:
        payload, ok_json = {}, False
    check("工具 web_search_deepseek 返回合法 JSON", ok_json, raw[:100])
    if ok_json:
        check("工具返回 results 数组", isinstance(payload.get("results"), list),
              str(type(payload.get("results"))))
        check("工具返回带 citation 说明", "citation" in payload)
        if payload.get("results"):
            row = payload["results"][0]
            check("结果含 index/title/url", {"index", "title", "url"} <= set(row))
            check("index 为 ref.n 形式", "." in str(row["index"]), str(row["index"]))
            check("summary 已按配置截断",
                  len(row.get("summary") or "") <= plugin_cfg["summary_max_chars"])
            check("未开启 need_content 时不带 content 字段", "content" not in row)

    # 工具 2：抓取
    page_txt = await plugin.tool_fetch_page(
        event, url="https://api-docs.deepseek.com/zh-cn/guides/search", max_chars=800
    )
    check("工具 deepseek_fetch_web_page 返回 Title/URL/正文",
          page_txt.startswith("Title:") and "URL:" in page_txt, page_txt[:80])
    check("抓取正文按 max_chars 截断", len(page_txt) < 2000, f"{len(page_txt)} 字符")

    # 工具 3：参数错误回传可读信息
    err = await plugin.tool_web_search(event, query="   ")
    check("空 query 返回中文提示", "参数不合法" in err, err[:60])

    # 命令：/dsearch
    out_ds = [x async for x in plugin.cmd_dsearch(FakeEvent("/dsearch DeepSeek V4"), "DeepSeek V4")]
    check("/dsearch 产出消息", len(out_ds) == 1 and out_ds[0][0] == "plain")
    text = out_ds[0][1] if out_ds else ""
    check("/dsearch 回复含标题与链接",
          "DeepSeek 搜索" in text and "http" in text, text[:80])

    # 命令：/dsearch --count 2
    out_c = [x async for x in plugin.cmd_dsearch(FakeEvent("/dsearch AI --count 2"), "AI --count 2")]
    text_c = out_c[0][1] if out_c else ""
    check("/dsearch 支持 --count", "共 2 条" in text_c or "共 1 条" in text_c, text_c[:40])

    # 命令：/dfetch
    out_df = [x async for x in plugin.cmd_dfetch(
        FakeEvent("/dfetch https://api-docs.deepseek.com/zh-cn/guides/search"),
        "https://api-docs.deepseek.com/zh-cn/guides/search",
    )]
    text_f = out_df[0][1] if out_df else ""
    check("/dfetch 回复含标题与正文", "📄" in text_f and len(text_f) > 100, text_f[:60])

    # 命令：/dsearch 空参数 → 用法提示
    out_empty = [x async for x in plugin.cmd_dsearch(FakeEvent("/dsearch"), "")]
    check("/dsearch 空参数给出用法", "用法" in (out_empty[0][1] if out_empty else ""))

    # 提示词注入
    class FakeReq:
        system_prompt = "你是一个助手。"

    req = FakeReq()
    await plugin.inject_tool_hint(event, req)
    check("提示词注入包含两个工具名",
          "web_search_deepseek" in req.system_prompt
          and "deepseek_fetch_web_page" in req.system_prompt)
    await plugin.inject_tool_hint(event, req)
    check("提示词注入幂等（不重复追加）",
          req.system_prompt.count("[DeepSeek 联网工具]") == 1,
          f"标记出现 {req.system_prompt.count('[DeepSeek 联网工具]')} 次")

    # 关闭开关后工具应拒绝
    plugin.config["enable_search_tool"] = False
    denied = await plugin.tool_web_search(event, query="test")
    check("关闭 enable_search_tool 后工具拒绝调用",
          "enable_search_tool=false" in denied, denied[:50])
    plugin.config["enable_search_tool"] = True

    await plugin.terminate()


asyncio.run(integration())

print()
print("=" * 68)
print(f"结果：{len(PASS)} PASS / {len(FAIL)} FAIL")
if FAIL:
    print("失败项：")
    for name in FAIL:
        print("  -", name)
    sys.exit(1)
print("全部通过 ✅")
