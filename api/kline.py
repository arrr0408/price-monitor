"""
Vercel Serverless Function - K线数据 API
支持分时(5)/日K(240)/周K(1200)/月K(4800)
"""
import json
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import requests

KLINE_URL = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
SCALE_DEFAULTS = {"5": "240", "240": "120", "1200": "52", "4800": "36"}
CACHE = {}
CACHE_TTL = {"5": 5, "240": 5, "1200": 3600, "4800": 3600}


def _agg_kline(items, interval):
    if not items:
        return []
    result = []
    bucket = None
    for d in items:
        day = d.get("day", "")
        if interval == "week":
            from datetime import datetime
            try:
                dt = datetime.strptime(day, "%Y-%m-%d")
                key = f"{dt.isocalendar()[0]}-W{dt.isocalendar()[1]:02d}"
            except Exception:
                key = day[:7]
        else:
            key = day[:7]

        if bucket is None or bucket["key"] != key:
            if bucket:
                result.append(bucket["data"])
            bucket = {"key": key, "data": {
                "day": key,
                "open": float(d["open"]),
                "high": float(d["high"]),
                "low": float(d["low"]),
                "close": float(d["close"]),
                "volume": str(float(d.get("volume", 0) or 0)),
            }}
        else:
            bd = bucket["data"]
            bd["high"] = str(max(float(bd["high"]), float(d["high"])))
            bd["low"] = str(min(float(bd["low"]), float(d["low"])))
            bd["close"] = d["close"]
            bd["volume"] = str(float(bd.get("volume", 0) or 0) + (float(d.get("volume", 0) or 0)))
    if bucket:
        result.append(bucket["data"])
    closes = [float(r["close"]) for r in result]
    for i, r in enumerate(result):
        r["ma_price5"] = str(round(sum(closes[max(0,i-4):i+1]) / min(i+1, 5), 2))
        r["ma_price10"] = str(round(sum(closes[max(0,i-9):i+1]) / min(i+1, 10), 2))
        r["ma_price20"] = str(round(sum(closes[max(0,i-19):i+1]) / min(i+1, 20), 2))
    return result


def fetch_kline(code, scale, datalen):
    fetch_code = "gjs_Au9999" if code == "computed_jicun" else code
    cache_key = f"{fetch_code}:{scale}:{datalen}"
    ttl = CACHE_TTL.get(scale, 5)
    cached = CACHE.get(cache_key)
    if cached and time.time() - cached["ts"] < ttl:
        return cached["data"]

    if scale in ("1200", "4800"):
        interval = "week" if scale == "1200" else "month"
        day_count = 250 if interval == "week" else 750
        try:
            resp = requests.get(KLINE_URL, params={
                "symbol": fetch_code, "scale": 240, "ma": "no",
                "datalen": str(day_count),
            }, headers={"Referer": "https://finance.sina.com.cn"}, timeout=10)
            resp.encoding = "utf-8"
            raw = resp.json()
            items = _agg_kline(raw if isinstance(raw, list) else [], interval)
            if datalen and datalen != str(day_count):
                items = items[-int(datalen):]
        except Exception as e:
            return {"error": str(e), "items": []}
    else:
        try:
            resp = requests.get(KLINE_URL, params={
                "symbol": fetch_code, "scale": scale, "ma": "5,10,20",
                "datalen": datalen,
            }, headers={"Referer": "https://finance.sina.com.cn"}, timeout=10)
            resp.encoding = "utf-8"
            data = resp.json()
            items = data if isinstance(data, list) else []
        except Exception as e:
            return {"error": str(e), "items": []}

    result = {"items": items, "code": code, "fetch_code": fetch_code, "scale": scale}
    CACHE[cache_key] = {"data": result, "ts": time.time()}
    return result


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            code = params.get("code", [""])[0].strip()
            scale = params.get("scale", ["240"])[0]
            datalen = params.get("datalen", [SCALE_DEFAULTS.get(scale, "120")])[0]

            if not code:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps({"error": "missing code"}).encode())
                return

            body = fetch_kline(code, scale, datalen)

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(json.dumps(body, ensure_ascii=False).encode())
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())
