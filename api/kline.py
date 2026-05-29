"""
Vercel Serverless Function - K线数据 API
"""
import json
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import requests

KLINE_URL = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"


def fetch_kline(code, days="120"):
    fetch_code = "gjs_Au9999" if code == "computed_jicun" else code

    try:
        resp = requests.get(KLINE_URL, params={
            "symbol": fetch_code,
            "scale": 240,
            "ma": "5,10,20",
            "datalen": days,
        }, headers={"Referer": "https://finance.sina.com.cn"}, timeout=10)
        resp.encoding = "utf-8"
        data = resp.json()
        if not isinstance(data, list):
            data = []
        return {"items": data, "code": code, "fetch_code": fetch_code}
    except Exception as e:
        return {"error": str(e), "items": []}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            code = params.get("code", [""])[0].strip()
            days = params.get("days", ["120"])[0]

            if not code:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps({"error": "missing code"}).encode())
                return

            body = fetch_kline(code, days)

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
