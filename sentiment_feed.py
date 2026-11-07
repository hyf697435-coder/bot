"""
市场情绪与重大新闻切片采集器 (Market Sentiment & News Feed)

数据源：
1. Alternative.me 恐慌贪婪指数 (Fear & Greed Index)
2. Cointelegraph 突发新闻 RSS (Breaking News Headlines)

特性：
- 本地文件持久化缓存 (默认 TTL 1小时 / 3600秒)，主交易循环毫秒级零延迟读取。
- 双通道网络获取：优先 Python urllib，网络或证书异常时无缝降级 curl。
- 绝不阻塞主循环，静默捕获任何网络或解析异常并优雅降级。
"""

import json
import logging
import os
import subprocess
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("sentiment_feed")

FNG_API_URL = "https://api.alternative.me/fng/?limit=1"
COINTELEGRAPH_RSS_URL = "https://cointelegraph.com/rss"
DEFAULT_CACHE_FILE = "news_sentiment_cache.json"
DEFAULT_TTL_SECONDS = 3600  # 1 小时缓存


def _fetch_url_text(url: str, timeout: int = 8) -> Optional[str]:
    """统一网络获取：优先 urllib，失败尝试 curl 回退。"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        logger.debug("[SentimentFeed] urllib 访问失败 | url=%s | err=%s | 尝试 curl 回退", url, e)

    # curl 回退 (特别适合 Android Termux 或系统证书异常环境)
    try:
        cmd = ["curl", "-s", "-A", headers["User-Agent"], "--connect-timeout", str(min(5, timeout)), "-m", str(timeout), url]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout + 2)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout
    except Exception as e:
        logger.debug("[SentimentFeed] curl 回退亦失败 | url=%s | err=%s", url, e)

    return None


def fetch_fear_and_greed(timeout: int = 8) -> Dict[str, Any]:
    """获取恐慌与贪婪指数。"""
    raw_text = _fetch_url_text(FNG_API_URL, timeout=timeout)
    if raw_text:
        try:
            data = json.loads(raw_text)
            if "data" in data and len(data["data"]) > 0:
                item = data["data"][0]
                val = int(item.get("value", 50))
                classification = str(item.get("value_classification", "Neutral"))
                return {
                    "value": val,
                    "classification": classification,
                }
        except Exception as e:
            logger.warning("[SentimentFeed] 解析恐慌贪婪指数 JSON 失败 | err=%s", e)

    return {"value": 50, "classification": "Neutral (Fallback)"}


def fetch_cointelegraph_headlines(limit: int = 6, timeout: int = 8) -> List[str]:
    """获取 Cointelegraph 最新头条。"""
    raw_xml = _fetch_url_text(COINTELEGRAPH_RSS_URL, timeout=timeout)
    headlines: List[str] = []
    if raw_xml:
        try:
            root = ET.fromstring(raw_xml)
            for item in root.findall(".//item"):
                title_elem = item.find("title")
                if title_elem is not None and title_elem.text:
                    title = title_elem.text.strip()
                    # 过滤无意义频道标题
                    if title.lower() in {"cointelegraph.com news", "cointelegraph"}:
                        continue
                    pub_date = item.find("pubDate")
                    date_str = f" ({pub_date.text.strip()})" if pub_date is not None and pub_date.text else ""
                    headlines.append(f"{title}{date_str}")
                    if len(headlines) >= limit:
                        break
        except Exception as e:
            logger.warning("[SentimentFeed] 解析 RSS XML 失败 | err=%s", e)

    return headlines


def get_market_sentiment(
    cache_file: str = DEFAULT_CACHE_FILE,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    force_refresh: bool = False,
    base_dir: Optional[str] = None
) -> Dict[str, Any]:
    """
    获取整合后的市场情绪切片（恐慌指数 + 重大新闻头条）。
    自带文件缓存机制，保证高频策略循环不被外网请求阻滞。
    """
    if base_dir:
        cache_path = os.path.join(base_dir, cache_file)
    else:
        cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), cache_file)

    now_ts = time.time()

    # 1. 尝试命中未过期缓存
    if not force_refresh and os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            cached_ts = cached.get("timestamp", 0)
            if now_ts - cached_ts < ttl_seconds and cached.get("status") == "ok":
                return cached
        except Exception as e:
            logger.warning("[SentimentFeed] 读取缓存异常 | path=%s | err=%s", cache_path, e)

    # 2. 缓存失效或强制刷新，执行拉取
    logger.info("[SentimentFeed] 正在刷新市场宏观情绪与新闻头条...")
    fng = fetch_fear_and_greed()
    headlines = fetch_cointelegraph_headlines(limit=6)

    updated_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    if headlines or fng.get("classification") != "Neutral (Fallback)":
        payload = {
            "status": "ok",
            "timestamp": now_ts,
            "updated_at": updated_iso,
            "fear_and_greed": fng,
            "top_headlines_24h": headlines,
        }
    else:
        # 无法拉取最新数据时，若存在旧缓存则退回使用旧缓存
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    old_data = json.load(f)
                logger.info("[SentimentFeed] 拉取失败，沿用历史情绪缓存")
                return old_data
            except Exception:
                pass

        payload = {
            "status": "fallback",
            "timestamp": now_ts,
            "updated_at": updated_iso,
            "fear_and_greed": fng,
            "top_headlines_24h": [
                "Market data connectivity limited; evaluate price action directly."
            ],
        }

    # 写入缓存
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("[SentimentFeed] 写入缓存文件失败 | path=%s | err=%s", cache_path, e)

    return payload


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
    print("Testing get_market_sentiment()...")
    res = get_market_sentiment(force_refresh=True)
    print(json.dumps(res, ensure_ascii=False, indent=2))
