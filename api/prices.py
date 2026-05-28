"""
Vercel Serverless Function - 价格数据 API
从新浪财经拉取行情，计算积存金价格
"""
import json
import os
import time
from http.server import BaseHTTPRequestHandler

import requests

SINA_URL = "https://hq.sinajs.cn/list="
SINA_HEADERS = {"Referer": "https://finance.sina.com.cn"}
OZ_TO_GRAM = 31.1035

# config.json 的内容嵌入（Vercel 环境直接读取文件）
CONFIG = {
    "jicun": {"spread_pct": 0.28, "display_name": "工银积存金"},
    "items": [
        {"name": "工银积存金", "code": "computed_jicun", "type": "computed", "note": "自动计算"},
        {"name": "国际金价", "code": "hf_XAU", "type": "sina_hf", "note": "XAU/USD 美元/盎司"},
        {"name": "美元/人民币", "code": "fx_susdcny", "type": "sina_forex", "note": "在岸汇率"},
        {"name": "上金所Au99.99", "code": "gjs_Au9999", "type": "sina_commodity", "note": "交易时段可用"},
    ]
}


def safe_float(s, default=0.0):
    try:
        return float(s)
    except (ValueError, TypeError):
        return default


def parse_hf_xau(fields, item):
    price = safe_float(fields[0])
    prev_close = safe_float(fields[3])
    return {
        "name": item["name"], "code": item["code"], "note": item.get("note", ""),
        "price": round(price, 2),
        "change": round(price - prev_close, 2),
        "change_pct": round((price - prev_close) / prev_close * 100, 2) if prev_close else 0,
        "high": safe_float(fields[4]), "low": safe_float(fields[5]),
        "open": safe_float(fields[7]), "prev_close": round(prev_close, 2),
        "time": time.strftime("%H:%M:%S"),
    }


def parse_fx_susdcny(fields, item):
    price = safe_float(fields[1])
    return {
        "name": item["name"], "code": item["code"], "note": item.get("note", ""),
        "price": round(price, 4),
        "change": safe_float(fields[10]),
        "change_pct": safe_float(fields[11]),
        "high": safe_float(fields[6]), "low": safe_float(fields[7]),
        "open": safe_float(fields[5]), "prev_close": safe_float(fields[8]),
        "time": time.strftime("%H:%M:%S"),
    }


def parse_gjs_au9999(fields, item):
    price = safe_float(fields[1])
    return {
        "name": item["name"], "code": item["code"], "note": item.get("note", ""),
        "price": round(price, 2),
        "change": safe_float(fields[2]), "change_pct": safe_float(fields[3]),
        "high": safe_float(fields[6]), "low": safe_float(fields[7]),
        "open": safe_float(fields[8]),
        "prev_close": safe_float(fields[9]) if len(fields) > 9 else 0,
        "time": time.strftime("%H:%M:%S"),
    }


def parse_stock(fields, item):
    price = safe_float(fields[3])
    prev_close = safe_float(fields[2])
    return {
        "name": item["name"], "code": item["code"], "note": item.get("note", ""),
        "price": round(price, 2),
        "change": round(price - prev_close, 2),
        "change_pct": round((price - prev_close) / prev_close * 100, 2) if prev_close else 0,
        "high": safe_float(fields[4]), "low": safe_float(fields[5]),
        "open": safe_float(fields[1]), "prev_close": round(prev_close, 2),
        "time": time.strftime("%H:%M:%S"),
    }


PARSERS = {
    "sina_hf": parse_hf_xau,
    "sina_forex": parse_fx_susdcny,
    "sina_commodity": parse_gjs_au9999,
    "sina_stock": parse_stock,
}


def fetch_sina():
    items = CONFIG["items"]
    codes = [it["code"] for it in items if it["code"] != "computed_jicun"]
    if not codes:
        return {}

    url = SINA_URL + ",".join(codes)
    try:
        resp = requests.get(url, headers=SINA_HEADERS, timeout=8)
        resp.encoding = "gbk"
        results = {}
        for line in resp.text.strip().split("\n"):
            for item in items:
                code = item["code"]
                if code == "computed_jicun":
                    continue
                if f"hq_str_{code}" in line:
                    parts = line.split('"')
                    if len(parts) >= 2 and parts[1].strip():
                        parser = PARSERS.get(item["type"])
                        if parser:
                            try:
                                r = parser(parts[1].split(","), item)
                                if r:
                                    results[code] = r
                            except Exception:
                                pass
                    break
        return results
    except Exception as e:
        print(f"fetch error: {e}")
        return {}


def compute_jicun(data):
    jicun_cfg = CONFIG.get("jicun", {})
    spread_pct = jicun_cfg.get("spread_pct", 0.28)

    xau = data.get("hf_XAU", {})
    usdcny = data.get("fx_susdcny", {})
    au9999 = data.get("gjs_Au9999", {})

    au_price = au9999.get("price", 0)
    xau_price = xau.get("price", 0)
    fx_rate = usdcny.get("price", 0)

    base_price = 0
    source = ""

    if au_price > 0:
        base_price = au_price * (1 + spread_pct / 100)
        source = "Au99.99"
    elif xau_price > 0 and fx_rate > 0:
        base_price = xau_price * fx_rate / OZ_TO_GRAM * (1 + spread_pct / 100)
        source = "XAU换算"

    if base_price <= 0:
        return None

    xau_change = xau.get("change", 0)
    jicun_change = round(xau_change * fx_rate / OZ_TO_GRAM * (1 + spread_pct / 100), 2)
    jicun_high = round((xau.get("high", 0) or 0) * fx_rate / OZ_TO_GRAM * (1 + spread_pct / 100), 2)
    jicun_low = round((xau.get("low", 0) or 0) * fx_rate / OZ_TO_GRAM * (1 + spread_pct / 100), 2)

    return {
        "name": jicun_cfg.get("display_name", "工银积存金"),
        "code": "computed_jicun",
        "note": f"{source} | 点差+{spread_pct}%",
        "price": round(base_price, 2),
        "change": jicun_change,
        "change_pct": xau.get("change_pct", 0),
        "high": jicun_high, "low": jicun_low,
        "open": 0, "prev_close": round(base_price - jicun_change, 2) if jicun_change else base_price,
        "time": time.strftime("%H:%M:%S"),
    }


def build_response():
    data = fetch_sina()

    # 积存金
    jicun = compute_jicun(data)
    items = []
    if jicun:
        items.append(jicun)

    # Au99.99 休市占位
    if "gjs_Au9999" not in data:
        data["gjs_Au9999"] = {
            "name": "上金所Au99.99", "code": "gjs_Au9999",
            "note": "休市 / 暂无数据",
            "price": 0, "change": 0, "change_pct": 0,
            "high": 0, "low": 0, "open": 0, "prev_close": 0,
            "time": time.strftime("%H:%M:%S"),
        }

    # 按 config 顺序排列
    code_order = [it["code"] for it in CONFIG["items"]]
    remaining = [v for k, v in data.items() if k not in ("computed_jicun",)]
    items.extend(remaining)
    items.sort(key=lambda x: code_order.index(x["code"]) if x["code"] in code_order else 999)

    return json.dumps({"items": items, "server_time": time.strftime("%H:%M:%S")}, ensure_ascii=False)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            body = build_response()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())
