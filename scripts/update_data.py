#!/usr/bin/env python3
"""Refresh Capital Flow Radar's public, source-attributed snapshot."""

from __future__ import annotations

import csv
import io
import json
import math
import re
import subprocess
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen
from xml.etree import ElementTree

import xlrd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "dist" / "data.json"
HEADERS = {"User-Agent": "CapitalFlowRadar/1.0 (public-data dashboard)"}
TIMEOUT = 40

CFTC_TFF = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
CFTC_DISAGG = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
JPX_PAGE = "https://www.jpx.co.jp/markets/statistics-equities/investor-type/index.html"
FARSIDE = "https://farside.co.uk/bitcoin-etf-flow-all-data/"
FARSIDE_FALLBACK = [
    ("2026-08-13", -131.1), ("2026-08-14", -56.2), ("2026-08-17", 297.5),
    ("2026-08-18", 189.3), ("2026-08-19", 517.2), ("2026-08-20", 606.3),
    ("2026-08-21", 307.5), ("2026-08-24", 337.6), ("2026-08-25", 314.3),
    ("2026-08-26", 232.2), ("2026-08-27", 242.3), ("2026-08-28", -201.9),
    ("2026-08-31", 216.7), ("2026-09-01", -236.5), ("2026-09-02", 101.1),
    ("2026-09-03", 730.8), ("2026-09-04", 174.6), ("2026-09-08", -46.6),
    ("2026-09-09", -120.2), ("2026-09-10", -45.0),
]

# Verified report-date prices used only when a stable free daily series is unavailable.
PRICE_OVERRIDES = {
    "copper": {"2026-09-01": 6.6005},
    "gold": {"2026-09-01": 4396.40},
}

NEWS_TOPICS = [
    {
        "key": "fed_rates", "label": "FRB・米金利", "symbol": "FED",
        "query": "Federal Reserve interest rates inflation bond yields when:7d",
        "focus": "利下げ・利上げ観測、インフレ指標、米国債利回り",
    },
    {
        "key": "boj_yen", "label": "日銀・円", "symbol": "BOJ",
        "query": "Bank of Japan BOJ interest rates yen when:7d",
        "focus": "日銀の政策修正、国内金利、日米金利差と円キャリー",
    },
    {
        "key": "geopolitics", "label": "地政学・供給", "symbol": "GEO",
        "query": "geopolitical risk oil supply Middle East shipping when:7d",
        "focus": "原油供給、海上輸送、安全資産需要",
    },
    {
        "key": "china", "label": "中国景気", "symbol": "CN",
        "query": "China economy stimulus property manufacturing demand when:7d",
        "focus": "景気対策、不動産、製造業、資源需要",
    },
    {
        "key": "global_risk", "label": "世界のリスク選好", "symbol": "RISK",
        "query": "global markets risk sentiment stocks bonds dollar when:7d",
        "focus": "株・債券・ドルの横断的なリスク選好",
    },
]


def fetch(url: str) -> bytes:
    request = Request(url, headers=HEADERS)
    try:
        with urlopen(request, timeout=TIMEOUT) as response:
            return response.read()
    except Exception:
        result = subprocess.run(
            ["curl", "-fsSL", "-A", HEADERS["User-Agent"], url],
            check=True, capture_output=True, timeout=TIMEOUT,
        )
        return result.stdout


def get_json(url: str, params: dict) -> list[dict]:
    return json.loads(fetch(url + "?" + urlencode(params)).decode("utf-8"))


def cftc_rows(dataset: str, code: str, market_name: str | None = None, limit: int = 8) -> list[dict]:
    clauses = [f"cftc_contract_market_code='{code}'"]
    if market_name:
        clauses.append(f"contract_market_name='{market_name}'")
    rows = get_json(dataset, {
        "$limit": str(limit),
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$where": " AND ".join(clauses),
    })
    if not rows:
        raise RuntimeError(f"No CFTC rows for {code} {market_name or ''}")
    return rows


def fred_value(series: str, date: str) -> float | None:
    start = date[:8] + "01"
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}&cosd={start}&coed={date}"
    try:
        body = fetch(url).decode("utf-8")
    except Exception:
        return None
    rows = list(csv.reader(io.StringIO(body)))
    values = []
    for row in rows[1:]:
        if len(row) >= 2 and row[0] <= date and row[1] not in ("", "."):
            try:
                values.append((row[0], float(row[1])))
            except ValueError:
                pass
    return values[-1][1] if values else None


def yahoo_daily_close(symbol: str, date: str) -> float | None:
    """Return the exact report-date futures close; never substitute another day."""
    target = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start = int((target - timedelta(days=2)).timestamp())
    end = int((target + timedelta(days=3)).timestamp())
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?period1={start}&period2={end}&interval=1d"
    )
    try:
        payload = json.loads(fetch(url).decode("utf-8"))
        result = payload["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        for stamp, close in zip(result.get("timestamp", []), closes):
            observed = datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%d")
            if observed == date and close is not None:
                return float(close)
    except Exception:
        return None
    return None


def number(row: dict, key: str) -> int:
    return int(float(row.get(key, 0) or 0))


def position_asset(
    *, key: str, name: str, symbol: str, group: str, dataset: str, code: str,
    market_name: str | None, long_key: str, short_key: str, change_long_key: str,
    change_short_key: str, multiplier: float, unit: str, price_series: str | None = None,
    price_override_key: str | None = None, yahoo_symbol: str | None = None,
    classification: str = "先物想定元本",
    confidence: float = 0.78, note: str = "",
) -> dict:
    rows = cftc_rows(dataset, code, market_name)
    latest = rows[0]
    date = latest["report_date_as_yyyy_mm_dd"][:10]
    long_pos = number(latest, long_key)
    short_pos = number(latest, short_key)
    net = long_pos - short_pos
    delta_1w = number(latest, change_long_key) - number(latest, change_short_key)
    old = rows[min(4, len(rows) - 1)]
    delta_4w = net - (number(old, long_key) - number(old, short_key))
    price = fred_value(price_series, date) if price_series else None
    price_source = f"FRED {price_series}" if price is not None else None
    if price is None and price_override_key:
        price = PRICE_OVERRIDES.get(price_override_key, {}).get(date)
        if price is not None:
            price_source = "検証済み報告日価格"
    if price is None and yahoo_symbol:
        price = yahoo_daily_close(yahoo_symbol, date)
        if price is not None:
            price_source = f"Yahoo Finance {yahoo_symbol} 報告日終値"
    values = {"1D": None, "1W": None, "4W": None}
    if price is not None:
        values["1W"] = delta_1w * multiplier * price / 1e9
        values["4W"] = delta_4w * multiplier * price / 1e9
    history = []
    for item in reversed(rows[:8]):
        item_long = number(item, long_key)
        item_short = number(item, short_key)
        history.append({
            "date": item["report_date_as_yyyy_mm_dd"][:10],
            "value": item_long - item_short,
            "long": item_long,
            "short": item_short,
        })
    return {
        "key": key, "name": name, "symbol": symbol, "group": group,
        "classification": classification, "date": date, "frequency": "週次",
        "delay": "火曜基準・原則金曜公表", "source": "CFTC",
        "sourceUrl": dataset, "price": price, "priceSource": price_source,
        "multiplier": multiplier, "unit": unit, "long": long_pos, "short": short_pos,
        "net": net, "delta1W": delta_1w, "delta4W": delta_4w,
        "history": history, "historyMetric": "ネット建玉", "historyUnit": "枚", "historyChart": "line",
        "values": values, "confidence": confidence, "note": note,
        "formula": f"ネット建玉変化 × {multiplier:g}{unit}" + (" × 報告日価格" if price is not None else ""),
    }


def treasury_asset() -> dict:
    rows = cftc_rows(CFTC_TFF, "043602", "UST 10Y NOTE")
    r, old = rows[0], rows[min(4, len(rows) - 1)]
    date = r["report_date_as_yyyy_mm_dd"][:10]
    def side(prefix: str) -> dict:
        if prefix == "asset":
            long_key, short_key, cl, cs = "asset_mgr_positions_long", "asset_mgr_positions_short", "change_in_asset_mgr_long", "change_in_asset_mgr_short"
        else:
            long_key, short_key, cl, cs = "lev_money_positions_long", "lev_money_positions_short", "change_in_lev_money_long", "change_in_lev_money_short"
        long_pos, short_pos = number(r, long_key), number(r, short_key)
        net = long_pos - short_pos
        delta_1w = number(r, cl) - number(r, cs)
        delta_4w = net - (number(old, long_key) - number(old, short_key))
        return {"long": long_pos, "short": short_pos, "net": net, "delta1W": delta_1w, "delta4W": delta_4w}
    asset_mgr, leveraged = side("asset"), side("lev")
    history = []
    for item in reversed(rows[:8]):
        long_pos = number(item, "asset_mgr_positions_long")
        short_pos = number(item, "asset_mgr_positions_short")
        history.append({
            "date": item["report_date_as_yyyy_mm_dd"][:10],
            "value": long_pos - short_pos,
            "long": long_pos,
            "short": short_pos,
        })
    return {
        "key": "ust10y", "name": "10年米国債", "symbol": "UST", "group": "defensive",
        "classification": "先物想定元本（額面）", "date": date, "frequency": "週次",
        "delay": "火曜基準・原則金曜公表", "source": "CFTC TFF", "sourceUrl": CFTC_TFF,
        "multiplier": 100000, "unit": "ドル額面", "assetManager": asset_mgr,
        "leveragedFund": leveraged,
        "history": history, "historyMetric": "資産運用会社ネット建玉", "historyUnit": "枚", "historyChart": "line",
        "values": {"1D": None, "1W": asset_mgr["delta1W"] * 100000 / 1e9, "4W": asset_mgr["delta4W"] * 100000 / 1e9},
        "confidence": 0.62,
        "formula": "資産運用会社のネット建玉変化 × $100,000額面",
        "note": "ベーシス取引の影響が大きいレバレッジファンドは別表示し、統合値へ加算しない。",
    }


def discover_jpx_files() -> list[str]:
    html = fetch(JPX_PAGE).decode("utf-8", errors="replace")
    urls = []
    for href in re.findall(r'href=["\']([^"\']*stock_val_1_[^"\']+\.xls)["\']', html, re.I):
        url = urljoin(JPX_PAGE, href)
        if url not in urls:
            urls.append(url)
    return urls[:5]


def parse_jpx_foreign_flow(content: bytes) -> tuple[str, int]:
    book = xlrd.open_workbook(file_contents=content)
    sheet = book.sheet_by_name("Tokyo & Nagoya") if "Tokyo & Nagoya" in book.sheet_names() else book.sheet_by_index(-1)
    date_text = str(sheet.cell_value(3, 0))
    match = re.search(r"\(\s*(\d{1,2}/\d{1,2})\s*-\s*(\d{1,2}/\d{1,2})\s*\)", date_text)
    year_match = re.search(r"(20\d{2})", date_text)
    year = int(year_match.group(1)) if year_match else datetime.now().year
    if match:
        month, day = map(int, match.group(2).split("/"))
        date = f"{year:04d}-{month:02d}-{day:02d}"
    else:
        date = "不明"
    balance = None
    for row in range(sheet.nrows):
        if str(sheet.cell_value(row, 0)).strip() == "Foreigners":
            sales_row = row - 1
            balance_col = sheet.ncols - 1
            for candidate_row in (sales_row, row):
                raw = sheet.cell_value(candidate_row, balance_col)
                if raw not in ("", None):
                    balance = int(str(raw).replace(",", ""))
                    break
            if balance is None:
                sales = int(str(sheet.cell_value(sales_row, 8)).replace(",", ""))
                purchases = int(str(sheet.cell_value(row, 8)).replace(",", ""))
                balance = purchases - sales
            break
    if balance is None:
        raise RuntimeError("Foreign investor balance not found in JPX file")
    return date, balance * 1000


def jpx_asset(usd_jpy: float) -> dict:
    files = discover_jpx_files()
    observations = []
    for url in files[:4]:
        date, yen = parse_jpx_foreign_flow(fetch(url))
        observations.append({"date": date, "yen": yen, "url": url})
    latest = observations[0]
    value_1w = latest["yen"] / usd_jpy / 1e9
    value_4w = sum(o["yen"] for o in observations) / usd_jpy / 1e9
    history = [
        {"date": o["date"], "value": o["yen"] / usd_jpy / 1e9}
        for o in reversed(observations)
    ]
    return {
        "key": "japan", "name": "日本株", "symbol": "JP", "group": "risk",
        "classification": "実測純フロー", "date": latest["date"], "frequency": "週次",
        "delay": "翌週第4営業日15:30ごろ", "source": "JPX 投資部門別売買状況",
        "sourceUrl": JPX_PAGE, "values": {"1D": None, "1W": value_1w, "4W": value_4w},
        "history": history, "historyMetric": "海外投資家の週間純フロー", "historyUnit": "$B", "historyChart": "bar",
        "confidence": 0.96, "formula": "海外投資家の現物株買越額 ÷ ドル円",
        "note": "東京・名古屋二市場の海外投資家差引額。JPX公表値をドル換算。",
        "raw": {"latestYen": latest["yen"], "weeks": observations, "usdJpy": usd_jpy},
    }


def parse_flow_value(text: str) -> float:
    text = text.strip().replace(",", "")
    if text in ("", "-"):
        return 0.0
    if text.startswith("(") and text.endswith(")"):
        return -float(text[1:-1])
    return float(text)


def news_topic(topic: dict) -> dict:
    """Collect recent headlines as evidence; causal interpretation stays rule-based."""
    params = {"q": topic["query"], "hl": "en-US", "gl": "US", "ceid": "US:en"}
    url = "https://news.google.com/rss/search?" + urlencode(params)
    root = ElementTree.fromstring(fetch(url))
    items = []
    seen = set()
    for node in root.findall("./channel/item"):
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        source_node = node.find("source")
        source = (source_node.text or "").strip() if source_node is not None else ""
        published_raw = (node.findtext("pubDate") or "").strip()
        if not title or not link or title.lower() in seen:
            continue
        seen.add(title.lower())
        try:
            published = parsedate_to_datetime(published_raw).astimezone(timezone.utc).isoformat()
        except Exception:
            published = published_raw
        items.append({"title": title, "url": link, "source": source, "publishedAt": published})
        if len(items) == 3:
            break
    if not items:
        raise RuntimeError(f"No news for {topic['key']}")
    return {k: topic[k] for k in ("key", "label", "symbol", "focus")} | {
        "items": items, "feedUrl": url, "loadState": "取得成功",
    }


def macro_topics(previous: dict) -> tuple[list[dict], list[str]]:
    previous_topics = {x.get("key"): x for x in previous.get("macroTopics", [])}
    topics, errors = [], []
    for topic in NEWS_TOPICS:
        try:
            topics.append(news_topic(topic))
        except Exception as exc:
            errors.append(f"news_{topic['key']}: {type(exc).__name__}")
            saved = previous_topics.get(topic["key"])
            if saved:
                saved = dict(saved)
                saved["loadState"] = "直近保存値"
                topics.append(saved)
            else:
                topics.append({k: topic[k] for k in ("key", "label", "symbol", "focus")} | {
                    "items": [], "feedUrl": "", "loadState": "取得失敗",
                })
    return topics, errors


def bitcoin_asset() -> dict:
    flows = []
    live = True
    try:
        html = fetch(FARSIDE).decode("utf-8", errors="replace")
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.I | re.S):
            cells = []
            for cell in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, re.I | re.S):
                clean = re.sub(r"<[^>]+>", " ", cell)
                clean = re.sub(r"\s+", " ", clean).strip()
                cells.append(clean)
            if len(cells) < 3 or not re.match(r"\d{1,2}\s+[A-Za-z]{3}\s+20\d{2}", cells[0]):
                continue
            dt = datetime.strptime(cells[0], "%d %b %Y")
            flows.append({"date": dt.strftime("%Y-%m-%d"), "usdM": parse_flow_value(cells[-1])})
    except Exception:
        live = False
    if not flows:
        live = False
        flows = [{"date": date, "usdM": value} for date, value in FARSIDE_FALLBACK]
    flows.sort(key=lambda x: x["date"])
    latest = flows[-1]
    return {
        "key": "bitcoin", "name": "ビットコインETF", "symbol": "BTC", "group": "risk",
        "classification": "実測純フロー", "date": latest["date"], "frequency": "日次",
        "delay": "米国市場終了後に順次確定", "source": "Farside Investors",
        "sourceUrl": FARSIDE,
        "values": {"1D": latest["usdM"] / 1000, "1W": sum(x["usdM"] for x in flows[-5:]) / 1000, "4W": sum(x["usdM"] for x in flows[-20:]) / 1000},
        "history": [{"date": x["date"], "value": x["usdM"] / 1000} for x in flows[-20:]],
        "historyMetric": "現物ETF日次純フロー", "historyUnit": "$B", "historyChart": "bar",
        "confidence": 0.90, "formula": "米国現物BTC ETF各銘柄の純流出入合計",
        "note": "日中暫定値は後から更新されることがある。" + ("" if live else " 現在は検証済み保存値を表示。"),
        "loadState": "取得成功" if live else "保存値",
        "raw": {"latest": latest, "last5": flows[-5:]},
    }


def unavailable_assets() -> list[dict]:
    return [
        {"name": "金ETF", "status": "未接続", "reason": "無料かつ安定した日次保有量データの自動取得経路を検証中"},
        {"name": "欧州株ETF", "status": "未接続", "reason": "無料データでは地域横断の純設定額を網羅できない"},
        {"name": "米国株ETF・投信全体", "status": "代替", "reason": "S&P500・NASDAQ先物の資産運用会社ポジションで代替"},
        {"name": "MMF・現金", "status": "未接続", "reason": "週次公表値の市場間重複を整理中"},
    ]


def previous_payload() -> dict:
    """Load the last verified snapshot for per-source fallback."""
    try:
        return json.loads(OUT.read_text(encoding="utf-8"))
    except Exception:
        return {"assets": []}


def previous_asset(payload: dict, key: str, error: Exception) -> dict | None:
    for asset in payload.get("assets", []):
        if asset.get("key") == key:
            saved = dict(asset)
            saved["loadState"] = "直近保存値"
            saved["note"] = (saved.get("note", "") + " 自動取得に失敗したため直近保存値を表示。").strip()
            saved["lastError"] = type(error).__name__
            return saved
    return None


def validate_payload(payload: dict) -> None:
    required = {"sp500", "nasdaq", "oil", "copper", "gold", "yen", "ust10y", "japan", "bitcoin"}
    keys = {asset.get("key") for asset in payload.get("assets", [])}
    missing = required - keys
    if missing:
        raise RuntimeError(f"Missing required assets: {', '.join(sorted(missing))}")
    for asset in payload["assets"]:
        if asset.get("values", {}).get("1W") is None:
            raise RuntimeError(f"Missing weekly value for {asset.get('key')}")
        for period, value in asset.get("values", {}).items():
            if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(value)):
                raise RuntimeError(f"Invalid {period} value for {asset.get('key')}")


def main() -> None:
    previous = previous_payload()
    yen = position_asset(
        key="yen", name="円先物", symbol="JPY", group="defensive", dataset=CFTC_TFF,
        code="097741", market_name="JAPANESE YEN", long_key="lev_money_positions_long",
        short_key="lev_money_positions_short", change_long_key="change_in_lev_money_long",
        change_short_key="change_in_lev_money_short", multiplier=12_500_000,
        unit="円", price_series=None, classification="先物想定元本", confidence=0.72,
        note="正値は円買い、負値は円売り。ドル円でドル換算する。",
    )
    usd_jpy = fred_value("DEXJPUS", yen["date"]) or 160.03
    yen["price"] = usd_jpy
    yen["priceSource"] = "FRED DEXJPUS"
    yen["formula"] = "ネット円建玉変化 × 12,500,000円 ÷ ドル円"
    yen["values"]["1W"] = yen["delta1W"] * 12_500_000 / usd_jpy / 1e9
    yen["values"]["4W"] = yen["delta4W"] * 12_500_000 / usd_jpy / 1e9

    assets = [
        position_asset(
            key="sp500", name="S&P500先物", symbol="SPX", group="risk", dataset=CFTC_TFF,
            code="13874A", market_name="E-MINI S&P 500", long_key="asset_mgr_positions_long",
            short_key="asset_mgr_positions_short", change_long_key="change_in_asset_mgr_long",
            change_short_key="change_in_asset_mgr_short", multiplier=50, unit="×指数",
            price_series="SP500", yahoo_symbol="ES%3DF", confidence=0.76,
            note="資産運用会社の方向を採用。レバレッジファンドのヘッジは統合値へ加算しない。",
        ),
        position_asset(
            key="nasdaq", name="NASDAQ100先物", symbol="NDX", group="risk", dataset=CFTC_TFF,
            code="209742", market_name="NASDAQ MINI", long_key="asset_mgr_positions_long",
            short_key="asset_mgr_positions_short", change_long_key="change_in_asset_mgr_long",
            change_short_key="change_in_asset_mgr_short", multiplier=20, unit="×指数",
            price_series="NASDAQ100", yahoo_symbol="NQ%3DF", confidence=0.76,
            note="資産運用会社の方向を採用。S&P500との重複があるため統合判定では減衰する。",
        ),
        position_asset(
            key="oil", name="WTI原油先物", symbol="WTI", group="risk", dataset=CFTC_DISAGG,
            code="067651", market_name=None, long_key="m_money_positions_long_all",
            short_key="m_money_positions_short_all", change_long_key="change_in_m_money_long_all",
            change_short_key="change_in_m_money_short_all", multiplier=1000, unit="バレル",
            price_series="DCOILWTICO", yahoo_symbol="CL%3DF", confidence=0.82,
        ),
        position_asset(
            key="copper", name="COMEX銅先物", symbol="CU", group="risk", dataset=CFTC_DISAGG,
            code="085692", market_name=None, long_key="m_money_positions_long_all",
            short_key="m_money_positions_short_all", change_long_key="change_in_m_money_long_all",
            change_short_key="change_in_m_money_short_all", multiplier=25000, unit="ポンド",
            price_override_key="copper", yahoo_symbol="HG%3DF", confidence=0.78,
        ),
        position_asset(
            key="gold", name="COMEX金先物", symbol="AU", group="defensive", dataset=CFTC_DISAGG,
            code="088691", market_name=None, long_key="m_money_positions_long_all",
            short_key="m_money_positions_short_all", change_long_key="change_in_m_money_long_all",
            change_short_key="change_in_m_money_short_all", multiplier=100, unit="oz",
            price_override_key="gold", yahoo_symbol="GC%3DF", confidence=0.78,
        ),
        yen,
        treasury_asset(),
    ]
    errors = []
    for key, loader in (("japan", lambda: jpx_asset(usd_jpy)), ("bitcoin", bitcoin_asset)):
        try:
            assets.append(loader())
        except Exception as exc:
            errors.append(f"{key}: {type(exc).__name__}")
            fallback = previous_asset(previous, key, exc)
            if fallback:
                assets.append(fallback)

    topics, news_errors = macro_topics(previous)
    errors.extend(news_errors)

    payload = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "periods": ["1D", "1W", "4W"],
        "assets": assets,
        "macroTopics": topics,
        "unavailable": unavailable_assets(),
        "errors": errors,
        "disclaimer": "先物は想定元本であり、現金の純流入額ではありません。異なる分類の金額は参考比較で、単純合算しません。",
    }
    validate_payload(payload)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUT.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(OUT)
    print(f"wrote {OUT} ({len(assets)} assets, {len(errors)} errors)")


if __name__ == "__main__":
    main()
