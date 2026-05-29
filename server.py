# -*- coding: utf-8 -*-
"""
实时价格监控面板 - 后端服务
支持本地运行和云端部署（Render/Railway）
"""
import json
import os
import socket
import sys
import time
import threading
from datetime import timezone, timedelta

# 北京时间
TZ = timezone(timedelta(hours=8))


def now_str(fmt="%H:%M:%S"):
    return time.strftime(fmt, time.gmtime(time.time() + 28800))

import requests
from flask import Flask, jsonify, render_template, request, send_from_directory

# Windows 控制台 UTF-8
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
SINA_URL = "https://hq.sinajs.cn/list="
SINA_HEADERS = {"Referer": "https://finance.sina.com.cn"}

# 常量
OZ_TO_GRAM = 31.1035

# 行情缓存
price_cache = {}
cache_lock = threading.Lock()
# 持久化历史收盘价（用于计算涨跌）
prev_close_store = {}
prev_close_lock = threading.Lock()


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_prev_closes():
    store_path = os.path.join(os.path.dirname(__file__), ".prev_close_cache.json")
    with prev_close_lock:
        try:
            with open(store_path, "w", encoding="utf-8") as f:
                json.dump(prev_close_store, f)
        except Exception:
            pass


def load_prev_closes():
    store_path = os.path.join(os.path.dirname(__file__), ".prev_close_cache.json")
    with prev_close_lock:
        try:
            if os.path.exists(store_path):
                with open(store_path, "r", encoding="utf-8") as f:
                    prev_close_store.update(json.load(f))
        except Exception:
            pass


def safe_float(s, default=0.0):
    try:
        return float(s)
    except (ValueError, TypeError):
        return default


# ═══════════════════════════════════════════
# 新浪行情解析器
# ═══════════════════════════════════════════

def _name(item, api_name):
    """自定义项未提供名称时（name==code），使用 API 返回的真实名称"""
    return api_name if item.get("name") == item["code"] else item.get("name", api_name)


def parse_hf_xau(fields, item):
    """hf_XAU 国际现货金: 最新价,买价,卖价,昨收,最高,最低,时间,开盘,?,0,0,0,日期,名称"""
    price = safe_float(fields[0])
    prev_close = safe_float(fields[3])
    return {
        "name": _name(item, "XAU"),
        "code": item["code"],
        "note": item.get("note", ""),
        "price": round(price, 2),
        "change": round(price - prev_close, 2),
        "change_pct": round((price - prev_close) / prev_close * 100, 2) if prev_close else 0,
        "high": safe_float(fields[4]),
        "low": safe_float(fields[5]),
        "open": safe_float(fields[7]),
        "prev_close": round(prev_close, 2),
        "time": now_str(),
    }


def parse_fx_susdcny(fields, item):
    """fx_susdcny 在岸人民币: 时间,最新价,...,最高,最低,昨收?,名称,涨跌额,涨跌幅,..."""
    price = safe_float(fields[1])
    prev_close = safe_float(fields[8])
    return {
        "name": _name(item, "USDCNY"),
        "code": item["code"],
        "note": item.get("note", ""),
        "price": round(price, 4),
        "change": safe_float(fields[10]),
        "change_pct": safe_float(fields[11]),
        "high": safe_float(fields[6]),
        "low": safe_float(fields[7]),
        "open": safe_float(fields[5]),
        "prev_close": round(prev_close, 4),
        "time": now_str(),
    }


def parse_gjs_au9999(fields, item):
    """gjs_Au9999 上金所: 名称,最新价,涨跌额,涨跌幅,买价,卖价,最高,最低,开盘,昨收,..."""
    price = safe_float(fields[1])
    prev_close = safe_float(fields[9]) if len(fields) > 9 else 0
    return {
        "name": _name(item, "Au99.99"),
        "code": item["code"],
        "note": item.get("note", ""),
        "price": round(price, 2),
        "change": safe_float(fields[2]),
        "change_pct": safe_float(fields[3]),
        "high": safe_float(fields[6]),
        "low": safe_float(fields[7]),
        "open": safe_float(fields[8]),
        "prev_close": round(prev_close, 2),
        "time": now_str(),
    }


def parse_stock(fields, item):
    """A股: 名称,今开,昨收,最新价,最高,最低,日期,时间,..."""
    price = safe_float(fields[3])
    prev_close = safe_float(fields[2])
    return {
        "name": _name(item, fields[0]),
        "code": item["code"],
        "note": item.get("note", ""),
        "price": round(price, 2),
        "change": round(price - prev_close, 2),
        "change_pct": round((price - prev_close) / prev_close * 100, 2) if prev_close else 0,
        "high": safe_float(fields[4]),
        "low": safe_float(fields[5]),
        "open": safe_float(fields[1]),
        "prev_close": round(prev_close, 2),
        "time": now_str(),
    }


PARSERS = {
    "sina_forex": parse_fx_susdcny,
    "sina_hf": parse_hf_xau,
    "sina_commodity": parse_gjs_au9999,
    "sina_stock": parse_stock,
}


def parse_line(line, item):
    if not line or "FAILED" in line:
        return None
    parts = line.split('"')
    if len(parts) < 2:
        return None
    data = parts[1]
    if not data or data.strip() == "":
        return None
    fields = data.split(",")
    parser = PARSERS.get(item.get("type", ""))
    if not parser:
        return None
    try:
        return parser(fields, item)
    except Exception as e:
        print(f"[WARN] parse {item['code']}: {e}")
        return None


# ═══════════════════════════════════════════
# 积存金价格计算（核心）
# ═══════════════════════════════════════════

def compute_jicun_price(config):
    """
    工银积存金价格计算：
    1. 优先使用上金所 Au99.99 现货价
    2. 其次使用 XAU/USD * USDCNY / 31.1035 换算
    3. 加上工行点差（可配置）
    """
    with cache_lock:
        au9999 = (price_cache.get("gjs_Au9999") or {}).copy()
        xau = (price_cache.get("hf_XAU") or {}).copy()
        usdcny = (price_cache.get("fx_susdcny") or {}).copy()

    au_price = au9999.get("price", 0)
    xau_price = xau.get("price", 0)
    fx_rate = usdcny.get("price", 0)

    jicun_config = config.get("jicun", {})
    spread_pct = jicun_config.get("spread_pct", 0.28)  # 默认点差比例%
    manual_price = jicun_config.get("manual_price", 0)  # 手动覆盖价格

    base_price = 0
    source = ""

    # 策略1: 手动价格（用户校准）
    if manual_price > 0:
        base_price = manual_price
        source = "手动校准"

    # 策略2: 上金所 Au99.99 + 点差
    elif au_price > 0:
        base_price = au_price * (1 + spread_pct / 100)
        source = "Au99.99"

    # 策略3: XAU 国际金价换算 + 点差
    elif xau_price > 0 and fx_rate > 0:
        spot_rmb = xau_price * fx_rate / OZ_TO_GRAM
        base_price = spot_rmb * (1 + spread_pct / 100)
        source = "XAU换算"

    if base_price <= 0:
        return None

    # 涨跌额基于 XAU 变化推算
    xau_change = xau.get("change", 0)
    jicun_change = round(xau_change * fx_rate / OZ_TO_GRAM * (1 + spread_pct / 100), 2)
    xau_change_pct = xau.get("change_pct", 0)
    jicun_high = round((xau.get("high", 0) or 0) * fx_rate / OZ_TO_GRAM * (1 + spread_pct / 100), 2)
    jicun_low = round((xau.get("low", 0) or 0) * fx_rate / OZ_TO_GRAM * (1 + spread_pct / 100), 2)

    # 记录历史收盘价用于计算涨跌
    code = "computed_jicun"
    now_date = time.strftime("%Y-%m-%d")
    with prev_close_lock:
        stored = prev_close_store.get(code, {})
        prev_close = stored.get("price", 0)
        stored_date = stored.get("date", "")

    if stored_date != now_date:
        # 新的一天，更新昨收
        prev_close = prev_close_store.get(code + "_last", {}).get("price", base_price)
        with prev_close_lock:
            prev_close_store[code] = {"price": base_price, "date": now_date}

    jicun_change_from_close = round(base_price - prev_close, 2) if prev_close else 0
    jicun_change_pct = round(jicun_change_from_close / prev_close * 100, 2) if prev_close else 0

    # 存今日首价
    with prev_close_lock:
        if code + "_last" not in prev_close_store or prev_close_store.get(code + "_last", {}).get("date") != now_date:
            prev_close_store[code + "_last"] = {"price": base_price, "date": now_date}
        # 每10分钟存一次当前价作为下次"昨收"
        prev_close_store[code + "_last"] = {"price": base_price, "date": now_date}

    return {
        "name": jicun_config.get("display_name", "工银积存金"),
        "code": code,
        "note": f"{source} | 点差+{spread_pct}%",
        "price": round(base_price, 2),
        "change": jicun_change if au_price <= 0 else (base_price - (au9999.get("prev_close", 0) or 0) * (1 + spread_pct / 100)),
        "change_pct": xau_change_pct if au_price <= 0 else au9999.get("change_pct", 0),
        "high": jicun_high,
        "low": jicun_low,
        "open": 0,
        "prev_close": round(base_price - jicun_change, 2) if jicun_change else base_price,
        "time": now_str(),
    }


# ═══════════════════════════════════════════
# 数据拉取
# ═══════════════════════════════════════════

def fetch_sina_prices(items):
    codes = [item["code"] for item in items if item["code"] != "computed_jicun"]
    if not codes:
        return

    url = SINA_URL + ",".join(codes)
    try:
        resp = requests.get(url, headers=SINA_HEADERS, timeout=10)
        resp.encoding = "gbk"
        text = resp.text

        for line in text.strip().split("\n"):
            for item in items:
                if item["code"] == "computed_jicun":
                    continue
                if f"hq_str_{item['code']}" in line:
                    result = parse_line(line, item)
                    if result:
                        with cache_lock:
                            price_cache[item["code"]] = result
                    break
    except Exception as e:
        print(f"[ERROR] fetch: {e}")


def fetch_loop():
    while True:
        try:
            config = load_config()
            fetch_sina_prices(config.get("items", []))
        except Exception as e:
            print(f"[ERROR] loop: {e}")
        time.sleep(5)


# ═══════════════════════════════════════════
# Flask 路由
# ═══════════════════════════════════════════

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": now_str()})


@app.route("/service-worker.js")
def service_worker():
    return send_from_directory("static", "service-worker.js", mimetype="application/javascript")


@app.route("/manifest.json")
def manifest():
    return send_from_directory("static", "manifest.json")


def guess_type(code):
    """根据代码前缀猜测品种类型"""
    if code.startswith("sh") or code.startswith("sz"):
        return "sina_stock"
    if code.startswith("hf_"):
        return "sina_hf"
    if code.startswith("fx_"):
        return "sina_forex"
    if code.startswith("gjs_"):
        return "sina_commodity"
    if code.startswith("nf_"):
        return "sina_stock"
    return "sina_stock"


@app.route("/api/prices")
def api_prices():
    config = load_config()

    # 解析前端传入的自选代码
    custom_codes_str = request.args.get("codes", "")
    custom_items = []
    if custom_codes_str:
        for code in custom_codes_str.split(","):
            code = code.strip()
            if code and code != "computed_jicun":
                custom_items.append({
                    "name": code, "code": code,
                    "type": guess_type(code), "note": "自选",
                })
        if custom_items:
            fetch_sina_prices(custom_items)

    with cache_lock:
        data = list(price_cache.values())

    # 计算积存金参考价
    jicun = compute_jicun_price(config)
    if jicun:
        data.insert(0, jicun)

    # 添加 Au99.99 休市占位
    existing_codes = {d["code"] for d in data}
    if "gjs_Au9999" not in existing_codes:
        data.append({
            "name": "上金所Au99.99",
            "code": "gjs_Au9999",
            "note": "休市 / 暂无数据",
            "price": 0, "change": 0, "change_pct": 0,
            "high": 0, "low": 0, "open": 0, "prev_close": 0,
            "time": now_str(),
        })

    # 去掉自定义代码中的未找到项，补占位
    for ci in custom_items:
        if ci["code"] not in existing_codes:
            data.append({
                "name": ci["code"], "code": ci["code"],
                "note": ci.get("note", "自选"),
                "price": 0, "change": 0, "change_pct": 0,
                "high": 0, "low": 0, "open": 0, "prev_close": 0,
                "time": now_str(),
            })

    # 按 config 顺序排列，自定义项放最后
    code_order = [item["code"] for item in config.get("items", [])]
    data.sort(key=lambda x: code_order.index(x["code"]) if x["code"] in code_order else 999)

    return jsonify({"items": data, "server_time": now_str()})


# ═══════════════════════════════════════════
# 用户状态同步（跨设备）
# ═══════════════════════════════════════════

USER_STATE_PATH = os.path.join(os.path.dirname(__file__), ".user_state.json")
user_state = {"hidden": [], "customs": []}
state_lock = threading.Lock()


def load_user_state():
    global user_state
    try:
        if os.path.exists(USER_STATE_PATH):
            with open(USER_STATE_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
                user_state["hidden"] = saved.get("hidden", [])
                user_state["customs"] = saved.get("customs", [])
    except Exception:
        pass


def save_user_state():
    try:
        with open(USER_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(user_state, f, ensure_ascii=False)
    except Exception:
        pass


@app.route("/api/state", methods=["GET", "POST"])
def api_state():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        with state_lock:
            if "hidden" in data:
                user_state["hidden"] = data["hidden"]
            if "customs" in data:
                user_state["customs"] = data["customs"]
            save_user_state()
        return jsonify({"ok": True})

    with state_lock:
        return jsonify({
            "hidden": user_state.get("hidden", []),
            "customs": user_state.get("customs", []),
        })


# ═══════════════════════════════════════════
# K线数据
# ═══════════════════════════════════════════

KLINE_CACHE = {}
KLINE_CACHE_TTL = 5  # 秒（分时/日线实时更新，周K月K在客户端缓存更久）

KLINE_URL = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"

# scale → 默认 datalen
SCALE_DEFAULTS = {"5": "240", "240": "120", "1200": "52", "4800": "36"}


def _agg_kline(items, interval):
    """将日线数据聚合为周线/月线"""
    if not items:
        return []
    result = []
    bucket = None
    for d in items:
        day = d.get("day", "")
        if interval == "week":
            # 按周分组：取 year+week 作为 key
            from datetime import datetime
            try:
                dt = datetime.strptime(day, "%Y-%m-%d")
                key = f"{dt.isocalendar()[0]}-W{dt.isocalendar()[1]:02d}"
            except Exception:
                key = day[:7]
        else:
            key = day[:7]  # 按月：YYYY-MM

        if bucket is None or bucket["key"] != key:
            if bucket:
                result.append(bucket["data"])
            bucket = {"key": key, "data": {
                "day": key,
                "open": float(d["open"]),
                "high": float(d["high"]),
                "low": float(d["low"]),
                "close": float(d["close"]),
                "volume": str(sum(float(x.get("volume", 0) or 0) for x in [d])),
            }}
        else:
            bd = bucket["data"]
            bd["high"] = str(max(float(bd["high"]), float(d["high"])))
            bd["low"] = str(min(float(bd["low"]), float(d["low"])))
            bd["close"] = d["close"]
            bd["volume"] = str(float(bd.get("volume", 0) or 0) + (float(d.get("volume", 0) or 0)))
    if bucket:
        result.append(bucket["data"])
    # 计算 MA（周K/月K用简化MA：取close计算）
    closes = [float(r["close"]) for r in result]
    for i, r in enumerate(result):
        r["ma_price5"] = str(round(sum(closes[max(0,i-4):i+1]) / min(i+1, 5), 2))
        r["ma_price10"] = str(round(sum(closes[max(0,i-9):i+1]) / min(i+1, 10), 2))
        r["ma_price20"] = str(round(sum(closes[max(0,i-19):i+1]) / min(i+1, 20), 2))
    return result


@app.route("/api/kline")
def api_kline():
    code = request.args.get("code", "").strip()
    scale = request.args.get("scale", "240")
    datalen = request.args.get("datalen") or SCALE_DEFAULTS.get(scale, "120")

    if not code:
        return jsonify({"error": "缺少 code 参数"}), 400

    # computed_jicun 映射到 Au99.99 的K线
    fetch_code = "gjs_Au9999" if code == "computed_jicun" else code

    # 检查缓存（周K/月K缓存更久）
    cache_key = f"{fetch_code}:{scale}:{datalen}"
    cache_ttl = 5 if scale in ("5", "240") else 3600
    cached = KLINE_CACHE.get(cache_key)
    if cached and time.time() - cached["ts"] < cache_ttl:
        return jsonify(cached["data"])

    # 周K/月K：从日线聚合
    if scale in ("1200", "4800"):
        interval = "week" if scale == "1200" else "month"
        day_count = {"week": 250, "month": 750}.get(interval, 250)
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
            return jsonify({"error": str(e), "items": []}), 500
    else:
        try:
            resp = requests.get(KLINE_URL, params={
                "symbol": fetch_code,
                "scale": scale,
                "ma": "5,10,20",
                "datalen": datalen,
            }, headers={"Referer": "https://finance.sina.com.cn"}, timeout=10)
            resp.encoding = "utf-8"
            data = resp.json()
            items = data if isinstance(data, list) else []
        except Exception as e:
            return jsonify({"error": str(e), "items": []}), 500

    result = {"items": items, "code": code, "fetch_code": fetch_code, "scale": scale}

    KLINE_CACHE[cache_key] = {"data": result, "ts": time.time()}
    return jsonify(result)


# ═══════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.254.254.254", 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def print_startup_info(port):
    local_ip = get_local_ip()
    local_url = f"http://{local_ip}:{port}"
    is_cloud = os.environ.get("RENDER") or os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("VERCEL")

    print()
    print("=" * 50)
    print("  实时价格监控面板已启动")
    print("=" * 50)
    print()

    if is_cloud:
        print("  云端模式 - 通过平台分配的域名访问")
    else:
        print(f"  本地访问: http://localhost:{port}")
        print(f"  手机访问: {local_url}")
        print()
        try:
            import qrcode
            qr = qrcode.QRCode(border=2)
            qr.add_data(local_url)
            qr.make(fit=True)
            qr.print_ascii(invert=True)
        except ImportError:
            pass

    print()
    print("  按 Ctrl+C 停止服务")
    print()


if __name__ == "__main__":
    load_prev_closes()
    load_user_state()

    PORT = int(os.environ.get("PORT", 5000))

    threading.Thread(target=fetch_loop, daemon=True).start()

    config = load_config()
    fetch_sina_prices(config.get("items", []))

    print_startup_info(PORT)

    try:
        app.run(host="0.0.0.0", port=PORT, debug=False)
    except KeyboardInterrupt:
        save_prev_closes()
        print("\n服务已停止")
