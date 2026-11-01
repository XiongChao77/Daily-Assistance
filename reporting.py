"""Collect Yahoo Finance quotes, research news, and render the HTML report."""

from __future__ import annotations

import json
import logging
import math
import re
import time
import tomllib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape

import plan_store


ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT / "config"
RESOURCES_DIR = ROOT / "resources"
LOG = logging.getLogger("daily")
PROMPTS = Environment(loader=FileSystemLoader(RESOURCES_DIR / "prompts"), autoescape=False)
HTML = Environment(loader=FileSystemLoader(RESOURCES_DIR / "templates"), autoescape=select_autoescape(["html", "j2"]))
MODEL_TIMEOUT = (10, 180)
MODEL_RETRIES = 3
MODEL_WORKERS = 2
MODEL_METRICS: list[dict] = []
MODEL_METRICS_LOCK = threading.Lock()


def record_model_call(section: str, attempt: int, started: float, status: str, data: dict | None = None) -> None:
    usage = (data or {}).get("usage", {})
    metric = {
        "section": section,
        "attempt": attempt,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "status": status,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }
    with MODEL_METRICS_LOCK:
        MODEL_METRICS.append(metric)
    LOG.info("AI 请求栏目=%s 尝试=%s 状态=%s 耗时=%sms tokens=%s",
             section, attempt, status, metric["duration_ms"], metric["total_tokens"])


def load_report_config() -> dict:
    config = json.loads((CONFIG_DIR / "report_config.json").read_text(encoding="utf-8"))
    for category in [*config["news_categories"], config["crypto_news"]]:
        if not 1 <= category["min_items"] <= category["max_items"] <= 10:
            raise ValueError(f"无效的栏目条数：{category['title']}")
    return config


def load_model_config() -> tuple[dict, str]:
    with (CONFIG_DIR / "config.toml").open("rb") as stream:
        settings = tomllib.load(stream)
    key = json.loads((CONFIG_DIR / "api_config.json").read_text(encoding="utf-8")).get("OPENAI_API_KEY")
    if not isinstance(key, str) or not key:
        raise ValueError("api_config.json 中缺少 OPENAI_API_KEY")
    provider = settings["model_providers"][settings["model_provider"]]
    if provider["wire_api"] != "responses":
        raise ValueError("仅支持 Responses API")
    return settings, key


def response_text(data: dict) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"].strip()
    chunks = []
    for item in data.get("output", []):
        if item.get("type") == "message":
            chunks.extend(part.get("text", "") for part in item.get("content", []) if part.get("type") == "output_text")
    return "\n".join(chunks).strip()


def parse_json(text: str) -> dict:
    start = text.find("{")
    if start < 0:
        raise ValueError("模型未返回 JSON 对象")
    value, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(value, dict):
        raise ValueError("模型 JSON 顶层应为对象")
    return value


def model_json(prompt: str, settings: dict, key: str, section: str = "unknown") -> dict:
    provider = settings["model_providers"][settings["model_provider"]]
    endpoint = provider["base_url"].rstrip("/") + "/responses"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    body = {
        "model": settings["model"], "input": prompt,
        "store": not settings.get("disable_response_storage", False),
        "tools": [{"type": "web_search"}], "tool_choice": "required",
    }
    for format_attempt in range(2):
        data = None
        for attempt in range(1, MODEL_RETRIES + 1):
            started = time.monotonic()
            try:
                response = requests.post(endpoint, headers=headers, json=body, timeout=MODEL_TIMEOUT)
            except requests.RequestException as exc:
                record_model_call(section, attempt, started, type(exc).__name__)
                if attempt == MODEL_RETRIES:
                    raise RuntimeError(f"模型请求失败：{type(exc).__name__}") from exc
                time.sleep(min(2 ** (attempt - 1), 8))
                continue
            status = str(response.status_code)
            if not response.ok:
                record_model_call(section, attempt, started, status)
                if response.status_code not in (408, 425, 429) and response.status_code < 500:
                    raise RuntimeError(f"模型请求失败：HTTP {response.status_code}")
                if attempt == MODEL_RETRIES:
                    raise RuntimeError(f"模型请求失败：HTTP {response.status_code}")
                retry_after = response.headers.get("Retry-After", "")
                try:
                    delay = min(max(float(retry_after), 0), 30) if retry_after else min(2 ** (attempt - 1), 8)
                except ValueError:
                    delay = min(2 ** (attempt - 1), 8)
                time.sleep(delay)
                continue
            try:
                data = response.json()
            except ValueError as exc:
                record_model_call(section, attempt, started, "invalid_json")
                if attempt == MODEL_RETRIES:
                    raise RuntimeError("模型返回了无效 JSON 响应") from exc
                time.sleep(min(2 ** (attempt - 1), 8))
                continue
            record_model_call(section, attempt, started, str(data.get("status", "ok")), data)
            break
        if data is None:
            raise RuntimeError("模型未返回有效响应")
        if data.get("status") in ("failed", "incomplete"):
            raise RuntimeError(f"模型响应状态：{data['status']}")
        try:
            return parse_json(response_text(data))
        except ValueError:
            if format_attempt:
                raise
            body["input"] = prompt + "\n\n请严格只返回有效 JSON，不含 Markdown 或解释。"
    raise RuntimeError("模型未返回有效 JSON")


def safe_url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    parts = urlsplit(value)
    return value if parts.scheme in ("https", "http") and parts.netloc else ""


def clean_text(value: object) -> str:
    result = str(value or "").strip()
    result = re.sub(r"\s*\(\[[^]]+\]\(https?://[^)]+\)\)", "", result)
    result = re.sub(r"\[([^]]+)\]\(https?://[^)]+\)", r"\1", result)
    result = result.replace("**", "")
    return result.strip()


def normalize_items(data: dict, category: dict) -> list[dict]:
    items = data.get("items")
    if not isinstance(items, list) or not category["min_items"] <= len(items) <= category["max_items"]:
        raise ValueError(f"{category['title']} 新闻条数不符合配置")
    result = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("新闻条目不是对象")
        title = clean_text(item.get("title"))
        body = clean_text(item.get("body"))
        if len(title) < 5 or len(body) < 70:
            raise ValueError(f"{category['title']} 存在内容过短的新闻")
        result.append({
            "title": title, "body": body,
            "impact": clean_text(item.get("impact")),
            "published_at": clean_text(item.get("published_at")),
            "source_name": clean_text(item.get("source_name")),
            "source_url": safe_url(item.get("source_url")),
        })
    return result


def generate_news(category: dict, generated_at: str, settings: dict, key: str, existing: list[dict] | None = None, on_batch=None) -> list[dict]:
    target = category["max_items"]
    items = list(existing or [])
    if len(items) > target:
        items = items[:target]
    while len(items) < target:
        batch_size = min(5, target - len(items))
        batch_category = {**category, "min_items": batch_size, "max_items": batch_size}
        prompt = PROMPTS.get_template("news_category.j2").render(
            category=batch_category, generated_at=generated_at,
            batch_no=len(items) // 5 + 1, batch_total=math.ceil(target / 5),
            avoid_titles=[item["title"] for item in items],
        )
        for attempt in range(2):
            try:
                batch = normalize_items(model_json(prompt, settings, key, category["id"]), batch_category)
                seen = {re.sub(r"[^\w]+", "", item["title"].casefold()) for item in items}
                batch_titles = [re.sub(r"[^\w]+", "", item["title"].casefold()) for item in batch]
                if any(title in seen for title in batch_titles) or len(set(batch_titles)) != len(batch_titles):
                    raise ValueError("新闻批次与前一批重复")
                items.extend(batch)
                if on_batch:
                    on_batch(items)
                break
            except ValueError as exc:
                if attempt:
                    raise
                LOG.warning("%s，要求模型重写 %s 第 %s 批", exc, category["title"], len(items) // 5 + 1)
                prompt += f"\n\n请严格输出 {batch_size} 条，每条 body 至少 70 个字，且不要重复此前标题。"
    return items


def generate_etf(generated_at: str, settings: dict, key: str) -> dict:
    prompt = PROMPTS.get_template("etf.j2").render(generated_at=generated_at)
    data = model_json(prompt, settings, key, "etf")
    for asset in ("btc", "eth"):
        item = data.get(asset)
        if not isinstance(item, dict) or not str(item.get("analysis", "")).strip():
            raise ValueError(f"ETF 板块缺少 {asset.upper()} 内容")
        item["source_url"] = safe_url(item.get("source_url"))
        item["funds"] = [fund for fund in item.get("funds", []) if isinstance(fund, dict)][:8]
    data["market_takeaway"] = str(data.get("market_takeaway", "")).strip()
    return normalize_etf(data)


def normalize_etf(data: dict) -> dict:
    for asset in ("btc", "eth"):
        item = data[asset]
        item["net_flow"] = clean_text(item.get("net_flow")).split("，", 1)[0].split("。", 1)[0]
        item["analysis"] = clean_text(item.get("analysis"))
        item["flow_date"] = clean_text(item.get("flow_date"))
        item["source_name"] = clean_text(item.get("source_name"))
        item["source_url"] = safe_url(item.get("source_url"))
        item["funds"] = [
            {"name": clean_text(fund.get("name")), "flow": clean_text(fund.get("flow"))}
            for fund in item.get("funds", []) if isinstance(fund, dict)
        ][:8]
    data["market_takeaway"] = clean_text(data.get("market_takeaway"))
    return data


def normalize_report_data(data: dict) -> dict:
    data.setdefault("crypto_display_items", 4)
    data.setdefault("generation_errors", [])
    data.setdefault("model_metrics", [])
    data.setdefault("plan_service_url", f"http://127.0.0.1:{plan_store.PORT}")
    data.setdefault("plan_service_token", plan_store.ensure_token())
    for category in data.get("news", []):
        category.setdefault("display_items", 4)
        for item in category.get("items", []):
            for field in ("title", "body", "impact", "published_at", "source_name"):
                item[field] = clean_text(item.get(field))
            item["source_url"] = safe_url(item.get("source_url"))
    for item in data.get("crypto_news", []):
        for field in ("title", "body", "impact", "published_at", "source_name"):
            item[field] = clean_text(item.get(field))
        item["source_url"] = safe_url(item.get("source_url"))
    for plan in data.get("plans", []):
        plan.setdefault("id", plan_store.plan_id(str(plan.get("source", "")), str(plan.get("title", ""))))
        plan.setdefault("done", False)
        plan.setdefault("progress", 0)
        plan.setdefault("note", "")
    normalize_etf(data["etf"])
    return data


def sparkline_points(values: list[float]) -> str:
    if len(values) < 2:
        return ""
    low, high = min(values), max(values)
    span = high - low or 1
    return " ".join(f"{i * 100 / (len(values)-1):.1f},{22 - (value-low) * 18 / span:.1f}" for i, value in enumerate(values))


def yahoo_quote(symbol: str) -> dict:
    encoded = quote(symbol, safe="")
    headers = {"User-Agent": "Mozilla/5.0 (compatible; DailyBrief/1.0)", "Accept": "application/json"}
    last_error = None
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            response = requests.get(
                f"https://{host}/v8/finance/chart/{encoded}",
                params={"range": "5d", "interval": "1d"}, headers=headers, timeout=20,
            )
            response.raise_for_status()
            chart = response.json()["chart"]["result"][0]
            meta = chart["meta"]
            closes = [float(value) for value in chart["indicators"]["quote"][0]["close"] if value is not None and math.isfinite(value)]
            if len(closes) < 2:
                raise ValueError("行情点数不足")
            price = float(meta.get("regularMarketPrice") or closes[-1])
            previous = closes[-2]
            change = price - previous
            return {
                "symbol": symbol, "price": price, "change": change,
                "percent": change / previous * 100 if previous else 0,
                "currency": meta.get("currency", ""),
                "as_of": datetime.fromtimestamp(meta["regularMarketTime"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                "points": sparkline_points(closes),
                "url": f"https://finance.yahoo.com/quote/{encoded}/",
                "available": True,
            }
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError, ZeroDivisionError) as exc:
            last_error = exc
            time.sleep(0.1)
    LOG.warning("Yahoo 行情不可用 %s：%s", symbol, type(last_error).__name__)
    return {"symbol": symbol, "available": False, "url": f"https://finance.yahoo.com/quote/{encoded}/"}


def collect_markets(groups: list[dict]) -> list[dict]:
    symbols = list(dict.fromkeys(item["symbol"] for group in groups for item in group["instruments"]))
    quotes = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(yahoo_quote, symbol): symbol for symbol in symbols}
        for future in as_completed(futures):
            quotes[futures[future]] = future.result()
    result = []
    for group in groups:
        instruments = []
        for item in group["instruments"]:
            quote_data = {**quotes[item["symbol"]], **item}
            if quote_data["available"]:
                precision = item.get("precision", 2)
                quote_data["price_label"] = f"{quote_data['price']:,.{precision}f}"
                quote_data["change_label"] = f"{quote_data['change']:+,.{precision}f}"
                quote_data["percent_label"] = f"{quote_data['percent']:+.2f}%"
                quote_data["direction"] = "up" if quote_data["change"] >= 0 else "down"
            instruments.append(quote_data)
        result.append({**group, "instruments": instruments})
    return result


def build_report_data(generated_at: datetime, plans: list[dict], draft_path: Path | None = None) -> dict:
    with MODEL_METRICS_LOCK:
        MODEL_METRICS.clear()
    config = load_report_config()
    settings, key = load_model_config()
    timestamp = generated_at.strftime("%Y-%m-%d %H:%M")
    draft = {"date": generated_at.date().isoformat(), "news": {}, "crypto_news": [], "etf": None, "complete": False}
    if draft_path and draft_path.exists():
        prior = json.loads(draft_path.read_text(encoding="utf-8"))
        if prior.get("date") == draft["date"] and not prior.get("complete"):
            draft = prior

    draft_lock = threading.Lock()
    generation_errors: dict[str, str] = {}
    results: dict[str, list[dict] | dict] = {}

    def save_draft_locked() -> None:
        if draft_path:
            plan_store.atomic_text(draft_path, json.dumps(draft, ensure_ascii=False, indent=2) + "\n")

    def generate_section(category: dict, cache_key: str, is_crypto: bool = False) -> list[dict]:
        with draft_lock:
            existing = list(draft.get("crypto_news", []) if is_crypto else draft.get("news", {}).get(cache_key, []))

        def save_items(items: list[dict]) -> None:
            with draft_lock:
                if is_crypto:
                    draft["crypto_news"] = items
                else:
                    draft.setdefault("news", {})[cache_key] = items
                save_draft_locked()

        LOG.info("生成新闻栏目：%s", category["title"])
        try:
            return generate_news(category, timestamp, settings, key, existing, save_items)
        except Exception as exc:
            LOG.exception("新闻栏目生成失败：%s", category["title"])
            with draft_lock:
                generation_errors[cache_key] = f"{category['title']}生成失败，本次保留已生成内容。"
                return list(draft.get("crypto_news", []) if is_crypto else draft.get("news", {}).get(cache_key, []))

    def empty_etf() -> dict:
        return {
            "btc": {"flow_date": "暂无数据", "net_flow": "本次未能获取", "funds": [], "analysis": "ETF 栏目生成失败，将在下次运行时重试。", "source_name": "", "source_url": ""},
            "eth": {"flow_date": "暂无数据", "net_flow": "本次未能获取", "funds": [], "analysis": "ETF 栏目生成失败，将在下次运行时重试。", "source_name": "", "source_url": ""},
            "market_takeaway": "ETF 栏目生成失败，将在下次运行时重试。",
        }

    def generate_etf_section() -> dict:
        with draft_lock:
            cached = draft.get("etf")
        if cached:
            return cached
        LOG.info("生成 ETF 板块")
        try:
            generated = generate_etf(timestamp, settings, key)
            with draft_lock:
                draft["etf"] = generated
                save_draft_locked()
            return generated
        except Exception:
            LOG.exception("ETF 板块生成失败")
            with draft_lock:
                generation_errors["etf"] = "BTC／ETH ETF 生成失败，将在下次运行时重试。"
            return empty_etf()

    jobs = {}
    with ThreadPoolExecutor(max_workers=MODEL_WORKERS, thread_name_prefix="ai-section") as pool:
        for category in config["news_categories"]:
            jobs[pool.submit(generate_section, category, category["id"])] = category["id"]
        crypto_category = config["crypto_news"]
        jobs[pool.submit(generate_section, crypto_category, "crypto_news", True)] = "crypto_news"
        jobs[pool.submit(generate_etf_section)] = "etf"
        for future in as_completed(jobs):
            results[jobs[future]] = future.result()

    news = [{**category, "items": results.get(category["id"], [])} for category in config["news_categories"]]
    crypto = results.get("crypto_news", [])
    etf = results.get("etf") or empty_etf()
    model_metrics = list(MODEL_METRICS)
    known_input = sum(metric["input_tokens"] for metric in model_metrics if isinstance(metric["input_tokens"], int))
    known_output = sum(metric["output_tokens"] for metric in model_metrics if isinstance(metric["output_tokens"], int))
    LOG.info("AI 汇总：%s 次请求，输入 tokens=%s，输出 tokens=%s",
             len(model_metrics), known_input, known_output)
    LOG.info("获取 Yahoo Finance 行情")
    markets = collect_markets(config["market_groups"])
    data = {
        "date": generated_at.date().isoformat(),
        "generated_at": generated_at.strftime("%Y-%m-%d %H:%M:%S"),
        "news": news, "markets": markets, "crypto_news": crypto,
        "crypto_display_items": config["crypto_news"].get("display_items", 4),
        "etf": etf, "plans": plans,
        "generation_errors": [{"section": key, "message": message} for key, message in sorted(generation_errors.items())],
        "model_metrics": model_metrics,
        "plan_service_url": f"http://127.0.0.1:{plan_store.PORT}",
        "plan_service_token": plan_store.ensure_token(),
    }
    with draft_lock:
        # The caller marks the draft complete only after the HTML and JSON files save.
        draft["complete"] = False
        draft["generation_errors"] = data["generation_errors"]
        save_draft_locked()
    return normalize_report_data(data)


def render_saved(data: dict) -> str:
    return HTML.get_template("report.html.j2").render(**normalize_report_data(data))
