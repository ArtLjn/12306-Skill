"""12306 查票工具组 — train_store（自包含，零外部依赖）。

提供 3 个工具函数：
  train_query            — 直达票查询
  train_transfer_query   — 中转票查询
  train_smart_query      — 智能综合查询（直达+中转+多买几站+补票+评分）

通过 ToolRegistry 动态加载，供主 Agent dispatch 调用。
所有辅助逻辑（车站查询、API 客户端、价格解析、评分）内联于此文件，无需额外模块。
"""

from __future__ import annotations

import json
import logging
import random
import re
import ssl
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Coroutine
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

ToolExecutor = Callable[[dict], Coroutine[Any, Any, str]]

__all__ = ["DEFINITIONS", "EXECUTORS", "DISPATCH"]
# ════════════════════════════════════════
# 车站电报码查询（原 _station.py）
# ════════════════════════════════════════

_STATION_URL = "https://kyfw.12306.cn/otn/resources/js/framework/station_name.js"
_CACHE_FILE = Path(__file__).parent.parent / "data" / "station_cache.json"

_station_map: dict[str, str] | None = None
_station_lock = threading.Lock()

def _load_station_cache() -> dict[str, str]:
    """从本地 JSON 缓存加载站名映射。"""
    if _CACHE_FILE.exists():
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def _fetch_station_online() -> dict[str, str]:
    """从 12306 在线拉取站名映射并更新本地缓存。"""
    try:
        req = Request(
            _STATION_URL,
            headers={"User-Agent": "Mozilla/5.0 (compatible; TrainQuery/1.0)"},
        )
        with urlopen(req, timeout=10, context=_SSL_CTX) as resp:
            text = resp.read().decode("utf-8")

        station_map: dict[str, str] = {}
        for item in text.split("@"):
            parts = item.split("|")
            if len(parts) >= 4:
                name, code = parts[1], parts[2]
                if name and code:
                    station_map[name] = code

        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(station_map, f, ensure_ascii=False)
        return station_map
    except (URLError, OSError) as e:
        logger.warning("拉取车站数据失败: %s", e)
        return {}

def _get_station_code(name: str) -> str | None:
    """获取车站电报码，缓存未命中时尝试在线刷新（线程安全）。"""
    global _station_map
    with _station_lock:
        if _station_map is None:
            _station_map = _load_station_cache()
        code = _station_map.get(name)
        if code:
            return code
    # 在线刷新
    online = _fetch_station_online()
    if online:
        with _station_lock:
            _station_map.update(online)
        return _station_map.get(name)
    return None
# ════════════════════════════════════════
# 12306 API 客户端（原 _api.py）
# ════════════════════════════════════════

_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17.2",
]

_DIRECT_URL = "https://kyfw.12306.cn/otn/leftTicket/query"
_DIRECT_INIT = "https://kyfw.12306.cn/otn/leftTicket/init"
_TRANSFER_INIT = "https://kyfw.12306.cn/otn/lcQuery/init"
_TRANSFER_BASE = "https://kyfw.12306.cn"
_MAX_RETRIES = 3

# NOTE: 12306 官方证书链在某些环境下不完整，导致 ssl.SSLCertVerificationError。
# 作为查询类公开 API，此处关闭证书校验是务实选择，非敏感数据传输。
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

# 直达票 pipe-separated 字段索引
_FIELD_IDX = {
    "train_no": 2,
    "station_train_code": 3,
    "from_station_code": 6,
    "to_station_code": 7,
    "start_time": 8,
    "arrive_time": 9,
    "lishi": 10,
    "from_station_no": 16,
    "to_station_no": 17,
    "gr_num": 21,
    "rw_num": 23,
    "tz_num": 25,
    "yw_num": 28,
    "yz_num": 29,
    "ze_num": 30,
    "zy_num": 31,
    "swz_num": 32,
    "yp_info_new": 39,
}

# 席别 key → 中文名
_SEAT_NAMES: dict[str, str] = {
    "swz_num": "商务座",
    "zy_num": "一等座",
    "ze_num": "二等座",
    "rw_num": "软卧",
    "yw_num": "硬卧",
    "yz_num": "硬座",
    "tz_num": "特等座",
    "gr_num": "高级软卧",
}

# 席别代码 → 中文名（用于价格解码）
_SEAT_CODE_NAME: dict[str, str] = {
    "9": "商务座", "P": "特等座", "M": "一等座", "O": "二等座",
    "6": "高级软卧", "4": "软卧", "F": "动卧", "3": "硬卧",
    "1": "硬座", "A": "高级动卧", "7": "一等座", "8": "二等座",
}

# 席别字段 key → 可能的代码列表（用于字段名→票价映射）
_SEAT_KEY_CODES: dict[str, list[str]] = {
    "swz_num": ["9", "A", "P"],
    "zy_num": ["M", "7"],
    "ze_num": ["O", "8"],
    "rw_num": ["4", "6", "F"],
    "yw_num": ["3"],
}

# 车次代号前缀 → 类型
_TRAIN_TYPE_MAP: list[tuple[str, str]] = [
    ("G", "高铁"),
    ("C", "高铁"),
    ("D", "动车"),
    ("Z", "直达"),
    ("T", "特快"),
    ("K", "快速"),
]

def _random_ua() -> str:
    """随机选一个 User-Agent。"""
    return random.choice(_USER_AGENTS)

def _http_get(url: str, timeout: int = 15) -> str:
    """发起 GET 请求，返回响应文本（自动处理 UTF-8 BOM）。"""
    req = Request(url, headers={"User-Agent": _random_ua()})
    with urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        return resp.read().decode("utf-8-sig")

def _classify_train(code: str) -> str:
    """根据车次代号判断类型。"""
    for prefix, label in _TRAIN_TYPE_MAP:
        if code.startswith(prefix):
            return label
    return "其他"

def _parse_seats(parts: list[str]) -> dict[str, str]:
    """从 pipe-separated 字段中提取有余票的席别。"""
    seats: dict[str, str] = {}
    for key, name in _SEAT_NAMES.items():
        idx = _FIELD_IDX[key]
        val = parts[idx] if idx < len(parts) else ""
        if val and val not in ("无", "", "--"):
            seats[name] = val
    return seats

def _decode_yp_info(yp_info: str) -> dict[str, float]:
    """解码 yp_info_new 字段，提取各席别票价。

    每组 10 字符：[0]席别代码 [1:6]票价÷10 [6:10]余票数。
    """
    prices: dict[str, float] = {}
    for i in range(0, len(yp_info), 10):
        group = yp_info[i : i + 10]
        if len(group) < 10:
            break
        seat_code = group[0]
        try:
            prices[seat_code] = int(group[1:6]) / 10.0
        except ValueError:
            continue
    return prices

def _get_min_price(ticket: dict) -> float:
    """从解析后的直达票 dict 中获取关注席别的最低票价。"""
    prices = ticket.get("prices", {})
    if not prices:
        return 0.0
    for key, codes in _SEAT_KEY_CODES.items():
        seat_name = _SEAT_NAMES.get(key, "")
        if seat_name and seat_name in ticket.get("seats", {}):
            for code in codes:
                p = prices.get(code, 0)
                if p > 0:
                    return p
    valid = [p for p in prices.values() if p > 0]
    return min(valid) if valid else 0.0

def _lishi_to_minutes(lishi: str) -> int:
    """将历时字符串（如 '05:30'）转为分钟数。"""
    if not lishi or ":" not in lishi:
        return 0
    parts = lishi.split(":")
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError):
        return 0

# Cookie 管理（线程安全）

_direct_cookies: dict[str, str] = {}
_direct_cookie_lock = threading.Lock()

def _init_direct_cookies() -> None:
    """访问余票查询首页获取必要 Cookie。"""
    global _direct_cookies
    with _direct_cookie_lock:
        _direct_cookies = {}
    try:
        req = Request(_DIRECT_INIT, headers={"User-Agent": _random_ua()})
        with urlopen(req, timeout=10, context=_SSL_CTX) as resp:
            for header in resp.headers.get_all("Set-Cookie") or []:
                parts = header.split(";")[0]
                if "=" in parts:
                    k, v = parts.split("=", 1)
                    with _direct_cookie_lock:
                        _direct_cookies[k.strip()] = v.strip()
    except (URLError, OSError) as e:
        logger.warning("获取直达查询Cookie失败: %s", e)

# 直达票查询

def _query_direct(
    train_date: str,
    from_code: str,
    to_code: str,
) -> list[dict[str, Any]]:
    """查询直达票，返回车次列表。"""
    params = {
        "leftTicketDTO.train_date": train_date,
        "leftTicketDTO.from_station": from_code,
        "leftTicketDTO.to_station": to_code,
        "purpose_codes": "ADULT",
    }
    url = f"{_DIRECT_URL}?{urlencode(params)}"

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            if attempt == 1 or not _direct_cookies:
                _init_direct_cookies()

            req = Request(url, headers={"User-Agent": _random_ua()})
            with _direct_cookie_lock:
                if _direct_cookies:
                    req.add_header(
                        "Cookie",
                        "; ".join(f"{k}={v}" for k, v in _direct_cookies.items()),
                    )
            with urlopen(req, timeout=15, context=_SSL_CTX) as resp:
                text = resp.read().decode("utf-8-sig").strip()

            if not text.startswith("{"):
                logger.warning("直达查询返回非JSON，第%d次重试", attempt)
                with _direct_cookie_lock:
                    _direct_cookies.clear()
                if attempt < _MAX_RETRIES:
                    time.sleep(random.uniform(1, 2))
                continue

            body = json.loads(text)
            if not body.get("status"):
                msgs = body.get("messages", ["未知错误"])
                logger.warning("直达查询返回错误: %s", msgs)
                return []

            result_list = body.get("data", {}).get("result", [])
            return [_parse_direct_item(item) for item in result_list]

        except (URLError, OSError) as e:
            logger.warning("直达查询失败 (第%d次): %s", attempt, e)
            if attempt < _MAX_RETRIES:
                time.sleep(random.uniform(1, 2))
        except (ValueError, KeyError) as e:
            logger.error("直达查询解析失败: %s", e)
            return []

    return []

def _parse_direct_item(raw: str) -> dict[str, Any]:
    """解析单条直达票记录。"""
    parts = raw.split("|")
    result: dict[str, Any] = {}
    for field, idx in _FIELD_IDX.items():
        result[field] = parts[idx] if idx < len(parts) else ""

    code = result.get("station_train_code", "")
    result["train_type"] = _classify_train(code)
    result["seats"] = _parse_seats(parts)
    result["has_ticket"] = bool(result["seats"])

    # 票价解码
    yp_info = result.pop("yp_info_new", "")
    result["prices"] = _decode_yp_info(yp_info) if yp_info else {}
    return result

# 经停站查询

_ROUTE_URL = "https://kyfw.12306.cn/otn/czxx/queryByTrainNo"

def _query_route_stops(
    train_no: str,
    from_code: str,
    to_code: str,
    train_date: str,
) -> list[dict[str, Any]]:
    """查询列车经停站列表，复用直达查询 Cookie。"""
    params = {
        "train_no": train_no,
        "from_station_telecode": from_code,
        "to_station_telecode": to_code,
        "depart_date": train_date,
    }
    url = f"{_ROUTE_URL}?{urlencode(params)}"

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            req = Request(url, headers={"User-Agent": _random_ua()})
            with _direct_cookie_lock:
                if _direct_cookies:
                    req.add_header(
                        "Cookie",
                        "; ".join(f"{k}={v}" for k, v in _direct_cookies.items()),
                    )
            with urlopen(req, timeout=15, context=_SSL_CTX) as resp:
                body = json.loads(resp.read().decode("utf-8-sig"))
            if body.get("status") and isinstance(body.get("data"), dict):
                return body["data"].get("data", [])
            return []
        except (URLError, OSError, ValueError) as e:
            logger.warning("经停站查询失败 (第%d次): %s", attempt, e)
            if attempt < _MAX_RETRIES:
                time.sleep(random.uniform(1, 2))
    return []

def _find_stations_after(stops: list[dict], name: str, n: int = 3) -> list[dict]:
    """找到目标站之后的 n 个经停站（多买几站用）。"""
    for i, s in enumerate(stops):
        if s.get("station_name") == name:
            return stops[i + 1 : i + 1 + n]
    return []

def _find_stations_before(stops: list[dict], name: str, n: int = 3) -> list[dict]:
    """找到目标站之前的 n 个经停站，按离目标站从近到远排列（补票用）。"""
    for i, s in enumerate(stops):
        if s.get("station_name") == name:
            before = stops[max(0, i - n) : i]
            before.reverse()
            return before
    return []

# 中转票查询

_transfer_cookies: dict[str, str] = {}
_transfer_cookie_lock = threading.Lock()

def _http_get_transfer(url: str, timeout: int = 15) -> str:
    """带 Cookie 的 GET 请求，自动保存和发送 Cookie（线程安全）。"""
    req = Request(url, headers={"User-Agent": _random_ua()})
    with _transfer_cookie_lock:
        if _transfer_cookies:
            req.add_header(
                "Cookie",
                "; ".join(f"{k}={v}" for k, v in _transfer_cookies.items()),
            )
    with urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        for header in resp.headers.get_all("Set-Cookie") or []:
            parts = header.split(";")[0]
            if "=" in parts:
                k, v = parts.split("=", 1)
                with _transfer_cookie_lock:
                    _transfer_cookies[k.strip()] = v.strip()
        return resp.read().decode("utf-8")

def _query_transfer(
    train_date: str,
    from_code: str,
    to_code: str,
    middle_code: str = "",
) -> list[dict[str, Any]]:
    """查询中转票，返回中转方案列表。"""
    global _transfer_cookies
    with _transfer_cookie_lock:
        _transfer_cookies = {}

    # 初始化获取动态查询路径
    search_url = "/lcquery/queryU"
    try:
        init_text = _http_get_transfer(_TRANSFER_INIT)
        match = re.search(r"var\s+lc_search_url\s*=\s*'(.+?)'", init_text)
        if match:
            search_url = match.group(1)
    except (URLError, OSError) as e:
        logger.warning("中转初始化失败: %s", e)

    params = {
        "train_date": train_date,
        "from_station_telecode": from_code,
        "to_station_telecode": to_code,
        "middle_station": middle_code,
        "result_index": 0,
        "can_query": "Y",
        "isShowWZ": "N",
        "purpose_codes": "00",
        "channel": "E",
    }
    url = f"{_TRANSFER_BASE}{search_url}?{urlencode(params)}"

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            text = _http_get_transfer(url)
            body = json.loads(text)
            if not body.get("status"):
                logger.warning("中转查询返回错误")
                return []
            data = body.get("data")
            if not isinstance(data, dict):
                return []
            return data.get("middleList", [])
        except (URLError, OSError) as e:
            logger.warning("中转查询失败 (第%d次): %s", attempt, e)
            if attempt < _MAX_RETRIES:
                time.sleep(random.uniform(2, 4))
                with _transfer_cookie_lock:
                    _transfer_cookies = {}
                try:
                    init_text = _http_get_transfer(_TRANSFER_INIT)
                    m = re.search(r"var\s+lc_search_url\s*=\s*'(.+?)'", init_text)
                    if m:
                        search_url = m.group(1)
                    url = f"{_TRANSFER_BASE}{search_url}?{urlencode(params)}"
                except (URLError, OSError):
                    pass
        except (ValueError, KeyError) as e:
            logger.error("中转查询解析失败: %s", e)
            return []

    return []
# ════════════════════════════════════════
# 智能评分 & 替代站查询
# ════════════════════════════════════════

# 评分权重（满分 100）
_W_DUR, _W_TYPE, _W_CONV, _W_COST, _W_SEAT = 40, 30, 10, 10, 10

def _score_plan(plan: dict, max_dur: int, max_price: float) -> float:
    """综合评分：时长(40) + 类型(30) + 便利度(10) + 成本(10) + 席别(10)。"""
    if max_dur <= 0:
        max_dur = 1

    # 时长分（线性衰减）
    dur = _W_DUR * max(0, 1 - plan.get("duration_min", 0) / max_dur)

    # 类型分
    type_scores = {
        "direct": _W_TYPE,
        "buy_more": _W_TYPE * 0.83,
        "short_buy": _W_TYPE * 0.75,
        "transfer": _W_TYPE * 0.67,
    }
    typ = type_scores.get(plan.get("plan_type", ""), _W_TYPE * 0.5)

    # 便利度分
    extra = plan.get("extra_stations", 0)
    pt = plan.get("plan_type", "")
    conv = _W_CONV
    if pt == "buy_more":
        conv = max(0, _W_CONV - extra * 3)
    elif pt == "short_buy":
        conv = max(0, _W_CONV - extra * 2)

    # 成本分（线性衰减）
    p = plan.get("price", 0) or 0
    cost = _W_COST * max(0, 1 - p / max_price) if max_price > 0 else _W_COST * 0.5

    # 席别分
    seat = _W_SEAT if plan.get("seats") else 0

    return round(dur + typ + conv + cost + seat, 1)

def _query_alt_station(
    ticket: dict,
    train_date: str,
    from_code: str,
    to_code: str,
    to_name: str,
    mode: str,
    max_stations: int = 3,
) -> list[dict]:
    """查询多买几站或先上车后补票的替代方案。

    mode: "buy_more" 查目的站之后的站，"short_buy" 查之前的站。
    """
    train_no = ticket.get("train_no", "")
    code = ticket.get("station_train_code", "")

    stops = _query_route_stops(train_no, from_code, to_code, train_date)
    if not stops:
        return []

    if mode == "buy_more":
        candidates = _find_stations_after(stops, to_name, max_stations)
    else:
        candidates = _find_stations_before(stops, to_name, max_stations)

    results: list[dict] = []
    for stop in candidates:
        alt_name = stop.get("station_name", "")
        alt_code = _get_station_code(alt_name)
        if not alt_code:
            continue

        alt_tickets = _query_direct(train_date, from_code, alt_code)
        for alt in alt_tickets:
            if alt.get("station_train_code") == code and alt.get("has_ticket"):
                extra = abs(
                    int(ticket.get("to_station_no", "0") or "0")
                    - int(alt.get("to_station_no", "0") or "0")
                )
                results.append(_make_plan(
                    mode, code, ticket.get("start_time", ""),
                    ticket.get("arrive_time", ""), ticket.get("lishi", ""),
                    alt.get("seats", {}), alt.get("prices", {}),
                    _get_min_price(alt), extra, alt_name, to_name,
                ))
                break  # 同车次取第一个有票的即可
        time.sleep(1)

    return results
# ════════════════════════════════════════
# 日期解析
# ════════════════════════════════════════

def _resolve_date(date_str: str) -> str:
    """将自然语言日期转换为 YYYY-MM-DD 格式。

    支持：今天/明天/后天/大后天、YYYY-MM-DD、M月D日。
    """
    if not date_str:
        return ""
    today = date.today()
    relative = {"今天": 0, "明天": 1, "后天": 2, "大后天": 3}
    if date_str in relative:
        return (today + timedelta(days=relative[date_str])).isoformat()
    try:
        return date.fromisoformat(date_str).isoformat()
    except ValueError:
        pass
    m = re.match(r"(\d{1,2})月(\d{1,2})[日号]", date_str)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        try:
            return date(today.year, month, day).isoformat()
        except ValueError:
            pass
    return date_str
# ════════════════════════════════════════
# 工具定义（OpenAI Function Calling 格式）
# ════════════════════════════════════════

DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "train_query",
            "description": (
                "查询火车直达票。返回指定日期、出发站到到达站的所有直达车次及余票。"
                "支持按车次类型（高铁/动车/特快等）过滤。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "出发日期"},
                    "from_station": {"type": "string", "description": "出发站名称"},
                    "to_station": {"type": "string", "description": "到达站名称"},
                    "train_type": {"type": "string", "description": "车次类型过滤（可选）"},
                },
                "required": ["date", "from_station", "to_station"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "train_transfer_query",
            "description": (
                "查询火车中转票。当直达票售罄或无直达车次时查询中转方案。"
                "可指定中转站，不指定则自动推荐。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "出发日期"},
                    "from_station": {"type": "string", "description": "出发站名称"},
                    "to_station": {"type": "string", "description": "到达站名称"},
                    "middle_station": {"type": "string", "description": "中转站名称（可选）"},
                },
                "required": ["date", "from_station", "to_station"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "train_smart_query",
            "description": (
                "智能火车票查询。综合直达、中转、多买几站、先上车后补票四种策略，"
                "按时长、类型、便利度、成本、席别综合评分，推荐最优方案。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "出发日期"},
                    "from_station": {"type": "string", "description": "出发站名称"},
                    "to_station": {"type": "string", "description": "到达站名称"},
                    "train_type": {"type": "string", "description": "车次类型过滤（可选）"},
                    "max_alt_stations": {"type": "integer", "description": "多买/少买最多查几站（默认3）"},
                },
                "required": ["date", "from_station", "to_station"],
            },
        },
    },
]
# ════════════════════════════════════════
# Executor 实现
# ════════════════════════════════════════

async def train_query(args: dict) -> str:
    """查询直达票。"""
    date_str = _resolve_date(args.get("date", ""))
    from_name = args.get("from_station", "")
    to_name = args.get("to_station", "")
    train_type = args.get("train_type", "")

    if not date_str:
        return json.dumps({"error": "请提供出发日期"}, ensure_ascii=False)

    from_code = _get_station_code(from_name)
    to_code = _get_station_code(to_name)

    if not from_code:
        return json.dumps({"error": f"未找到车站: {from_name}"}, ensure_ascii=False)
    if not to_code:
        return json.dumps({"error": f"未找到车站: {to_name}"}, ensure_ascii=False)

    results = _query_direct(date_str, from_code, to_code)
    if train_type:
        results = [r for r in results if r.get("train_type") == train_type]
    has_ticket = [r for r in results if r.get("has_ticket")]

    return json.dumps({
        "date": date_str, "from": from_name, "to": to_name,
        "total": len(results), "has_ticket_count": len(has_ticket),
        "sold_out_count": len(results) - len(has_ticket),
        "trains": has_ticket[:10],
    }, ensure_ascii=False)

async def train_transfer_query(args: dict) -> str:
    """查询中转票。"""
    date_str = _resolve_date(args.get("date", ""))
    from_name = args.get("from_station", "")
    to_name = args.get("to_station", "")
    middle_name = args.get("middle_station", "")

    if not date_str:
        return json.dumps({"error": "请提供出发日期"}, ensure_ascii=False)

    from_code = _get_station_code(from_name)
    to_code = _get_station_code(to_name)
    middle_code = _get_station_code(middle_name) if middle_name else ""

    if not from_code:
        return json.dumps({"error": f"未找到车站: {from_name}"}, ensure_ascii=False)
    if not to_code:
        return json.dumps({"error": f"未找到车站: {to_name}"}, ensure_ascii=False)

    raw = _query_transfer(date_str, from_code, to_code, middle_code)

    def _seg_dict(seg: dict, prefix: str) -> dict:
        return {
            f"{prefix}_train": seg.get("station_train_code", ""),
            f"{prefix}_from": seg.get("from_station_name", ""),
            f"{prefix}_start": seg.get("start_time", ""),
            f"{prefix}_to": seg.get("to_station_name", ""),
            f"{prefix}_arrive": seg.get("arrive_time", ""),
        }

    routes = []
    for item in raw[:5]:
        trains = item.get("fullList", [])
        if len(trains) < 2:
            continue
        route = {
            "middle": item.get("middle_station_name", ""),
            "total_duration": item.get("all_lishi", ""),
            "wait_time": item.get("wait_time", ""),
        }
        route.update(_seg_dict(trains[0], "first"))
        route.update(_seg_dict(trains[1], "second"))
        routes.append(route)

    return json.dumps({
        "date": date_str, "from": from_name, "to": to_name,
        "total": len(raw), "routes": routes,
    }, ensure_ascii=False)

def _make_plan(
    plan_type: str, code: str, start: str, arrive: str,
    lishi: str, seats: dict, prices: dict, price: float,
    extra: int = 0, buy_to: str = "", target: str = "",
    **kw: Any,
) -> dict:
    """构造统一的方案 dict。"""
    return {
        "plan_type": plan_type,
        "station_train_code": code,
        "start_time": start,
        "arrive_time": arrive,
        "lishi": lishi,
        "duration_min": _lishi_to_minutes(lishi),
        "seats": seats,
        "prices": prices,
        "price": price,
        "extra_stations": extra,
        "buy_to_station": buy_to,
        "target_station": target,
        **kw,
    }

async def train_smart_query(args: dict) -> str:
    """智能查询：综合直达+中转+多买几站+先上车后补票，评分排序推荐。"""
    date_str = _resolve_date(args.get("date", ""))
    from_name = args.get("from_station", "")
    to_name = args.get("to_station", "")
    train_type = args.get("train_type", "")
    max_stations = int(args.get("max_alt_stations", 3))

    if not date_str:
        return json.dumps({"error": "请提供出发日期"}, ensure_ascii=False)

    from_code = _get_station_code(from_name)
    to_code = _get_station_code(to_name)
    if not from_code:
        return json.dumps({"error": f"未找到车站: {from_name}"}, ensure_ascii=False)
    if not to_code:
        return json.dumps({"error": f"未找到车站: {to_name}"}, ensure_ascii=False)

    plans: list[dict] = []

    # 1. 直达票
    direct = _query_direct(date_str, from_code, to_code)
    if train_type:
        direct = [d for d in direct if d.get("train_type") == train_type]

    for d in direct:
        if d.get("has_ticket"):
            plans.append(_make_plan(
                "direct", d.get("station_train_code", ""),
                d.get("start_time", ""), d.get("arrive_time", ""),
                d.get("lishi", ""), d.get("seats", {}),
                d.get("prices", {}), _get_min_price(d),
                buy_to=to_name, target=to_name,
            ))

    # 2. 中转票
    raw_transfers = _query_transfer(date_str, from_code, to_code)
    for item in raw_transfers[:5]:
        trains = item.get("fullList", [])
        if len(trains) < 2:
            continue
        first, second = trains[0], trains[1]
        plans.append(_make_plan(
            "transfer",
            f"{first.get('station_train_code', '')}+{second.get('station_train_code', '')}",
            first.get("start_time", ""), second.get("arrive_time", ""),
            item.get("all_lishi", ""), {}, 0, 0,
            middle_station=item.get("middle_station_name", ""),
            wait_time=item.get("wait_time", ""),
            buy_to=to_name, target=to_name,
        ))

    # 3. 对售罄车次尝试多买几站和先上车后补票
    sold_out = [d for d in direct if not d.get("has_ticket")]
    for ticket in sold_out[:5]:
        for mode in ("buy_more", "short_buy"):
            plans.extend(_query_alt_station(
                ticket, date_str, from_code, to_code, to_name, mode, max_stations,
            ))

    # 4. 评分排序
    durations = [p["duration_min"] for p in plans if p["duration_min"] > 0]
    max_dur = max(durations) if durations else 1
    prices = [p["price"] for p in plans if p.get("price", 0) > 0]
    max_price = max(prices) if prices else 0

    for p in plans:
        p["score"] = _score_plan(p, max_dur, max_price)
    plans.sort(key=lambda p: p["score"], reverse=True)

    return json.dumps({
        "date": date_str, "from": from_name, "to": to_name,
        "total": len(plans), "plans": plans[:10],
    }, ensure_ascii=False)
# ════════════════════════════════════════
# Executor 注册表
# ════════════════════════════════════════

EXECUTORS: dict[str, ToolExecutor] = {
    "train_query": train_query,
    "train_transfer_query": train_transfer_query,
    "train_smart_query": train_smart_query,
}
# ════════════════════════════════════════
# Dispatch
# ════════════════════════════════════════

_ACTION_TO_TOOL: dict[str, str] = {
    "query": "train_query",
    "transfer_query": "train_transfer_query",
    "smart_query": "train_smart_query",
}

async def train_dispatch(
    params: dict,
    execute_fn: Callable[[str, dict], Coroutine[Any, Any, str]],
) -> dict:
    """12306 查票结构化分发。"""
    action = params.get("action", "query")
    tool_name = _ACTION_TO_TOOL.get(action)

    if not tool_name:
        return {
            "reply": f"未知操作: {action}",
            "metadata": {"action": action, "error": "unknown_action"},
            "status": "failed",
        }

    tool_args = {k: v for k, v in params.items() if k != "action"}
    result_str = await execute_fn(tool_name, tool_args)
    result = json.loads(result_str) if result_str else {}

    is_failed = isinstance(result, dict) and bool(result.get("error"))
    reply = _format_reply(action, result, is_failed)

    return {
        "reply": reply,
        "metadata": result,
        "status": "failed" if is_failed else "success",
    }

def _format_reply(action: str, result: dict, is_failed: bool) -> str:
    """根据 action 类型格式化语音播报友好回复。"""
    if is_failed:
        return result.get("error", "查询失败，请稍后再试")
    formatter = _REPLY_FORMATTERS.get(action)
    if formatter:
        return formatter(result)
    return str(result)

# 回复格式化策略

def _format_query(r: dict) -> str:
    """直达票查询回复。"""
    total = r.get("total", 0)
    trains = r.get("trains", [])
    d, fr, to = r.get("date", ""), r.get("from", ""), r.get("to", "")

    if total == 0:
        return f"{d} {fr}到{to}没有直达车次，要不要帮您查一下中转方案？"

    has_count = r.get("has_ticket_count", 0)
    sold_count = r.get("sold_out_count", 0)
    parts = [f"{d} {fr}到{to}共{total}趟直达车次"]
    if sold_count > 0:
        parts.append(f"其中{has_count}趟有票，{sold_count}趟已售罄")
    else:
        parts.append("全部有票")

    if trains:
        parts.append("有票车次：")
        for t in trains[:5]:
            seat_str = "、".join(f"{k}{v}" for k, v in t.get("seats", {}).items())
            parts.append(
                f"{t.get('station_train_code', '')} {t.get('start_time', '')}发车"
                f"{t.get('arrive_time', '')}到，历时{t.get('lishi', '')}，{seat_str}"
            )

    return "。".join(parts)

def _format_transfer(r: dict) -> str:
    """中转票查询回复。"""
    routes = r.get("routes", [])
    from_name = r.get("from", "")
    to_name = r.get("to", "")
    d = r.get("date", "")

    if not routes:
        return f"{d} {from_name}到{to_name}没有找到中转方案"

    total = r.get("total", 0)
    parts = [
        f"{d} {from_name}到{to_name}共找到{total}个中转方案，"
        f"为您展示前{len(routes)}个"
    ]

    for i, route in enumerate(routes, 1):
        mid = route.get("middle", "")
        f1, f1s, f1a = route.get("first_train", ""), route.get("first_start", ""), route.get("first_arrive", "")
        f2, f2s, f2a = route.get("second_train", ""), route.get("second_start", ""), route.get("second_arrive", "")
        parts.append(
            f"方案{i}，经{mid}中转，总耗时{route.get('total_duration', '')}，"
            f"等待{route.get('wait_time', '')}。"
            f"第一程{f1}，{f1s}出发，{f1a}到{mid}。"
            f"第二程{f2}，{f2s}从{mid}出发，{f2a}到达"
        )

    return "。".join(parts)

def _format_smart(r: dict) -> str:
    """智能查询回复（语音播报友好格式）。"""
    plans = r.get("plans", [])
    d, fr, to = r.get("date", ""), r.get("from", ""), r.get("to", "")
    if not plans:
        return f"{d} {fr}到{to}没有找到任何方案"

    type_labels = {"direct": "直达", "transfer": "中转", "buy_more": "多买几站", "short_buy": "先上车后补票"}
    show = min(len(plans), 5)
    parts = [f"{d} {fr}到{to}共找到{r.get('total', 0)}个方案，推荐前{show}个"]

    for i, p in enumerate(plans[:5], 1):
        label = type_labels.get(p.get("plan_type", ""), "")
        seat_str = "、".join(f"{k}{v}" for k, v in p.get("seats", {}).items())
        line = (
            f"方案{i}（{label}，评分{p.get('score', 0)}）"
            f"{p.get('station_train_code', '')} {p.get('start_time', '')}出发"
            f"{p.get('arrive_time', '')}到，历时{p.get('lishi', '')}"
        )
        if seat_str:
            line += f"，{seat_str}"
        if p.get("buy_to_station") and p["buy_to_station"] != to:
            line += f"，需买票到{p['buy_to_station']}"
        parts.append(line)

    return "。".join(parts)

_REPLY_FORMATTERS: dict[str, Callable[[dict], str]] = {
    "query": _format_query,
    "transfer_query": _format_transfer,
    "smart_query": _format_smart,
}

DISPATCH = train_dispatch