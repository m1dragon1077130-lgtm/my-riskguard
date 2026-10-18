import os
import sys
import json
import time
import hmac
import hashlib
import urllib.request
import urllib.error

# خواندن کلیدها از Secrets گیت‌هاب (امن)
API_KEY = os.environ.get("TT_API_KEY")
API_SECRET = os.environ.get("TT_API_SECRET")

if not API_KEY or not API_SECRET:
    print("❌ خطا: TT_API_KEY یا TT_API_SECRET در Secrets گیت‌هاب تنظیم نشده.")
    sys.exit(1)

# تنظیمات ربات
MAX_LOSS_USD = 1.0          
REWARD_RATIO = 5.0          
TRIGGER_RATIO = 3.0         
LOCK_PROFIT_RATIO = 1.0     
MAX_RUN_TIME_SECONDS = 5 * 3600 + 50 * 60
CHECK_INTERVAL_SECONDS = 20                 
ONLY_ONE_TRADE = True
LOCK_STOP_LOSS = True

BASE_URL = "https://apiv2.thetruetrade.io"
FUTURES_PREFIX = "/futures"
STATE_FILE = "state.json"

def sign(secret: str, timestamp: str, method: str, uri: str) -> str:
    payload = f"{timestamp}{method}{uri}"
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()

def request(method: str, uri: str, body: dict = None):
    timestamp = str(int(time.time() * 1000))
    signature = sign(API_SECRET, timestamp, method.upper(), uri)
    url = BASE_URL + uri
    headers = {
        "X-API-Key": API_KEY,
        "X-Timestamp": timestamp,
        "X-Signature": signature,
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        print(f"خطای HTTP {e.code} در {method} {uri}: {e.read().decode(errors='replace')}")
        return None
    except Exception as e:
        print(f"خطا در {method} {uri}: {e}")
        return None

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f: return json.load(f)
        except Exception: return {}
    return {}

def save_state(state):
    try:
        with open(STATE_FILE, "w") as f: json.dump(state, f, indent=2)
    except Exception as e: print(f"خطا در ذخیره state: {e}")

def run_riskguard_cycle(state):
    response = request("GET", f"{FUTURES_PREFIX}/positions")
    if response is None: return

    positions = response.get("items", []) if isinstance(response, dict) else response
    open_positions = [p for p in positions if isinstance(p, dict) and p.get("status") == "OPENED" and p.get("isActive")]

    if not open_positions: return

    if ONLY_ONE_TRADE and len(open_positions) > 1:
        open_positions.sort(key=lambda p: (p.get("createdAt", ""), p.get("id")))
        oldest = open_positions[0]
        print(f"نگه داشتن قدیمی‌ترین: {oldest['id']} ({oldest['symbol']})")
        for p in open_positions[1:]:
            print(f"بستن پوزیشن جدیدتر {p['id']}")
            request("POST", f"{FUTURES_PREFIX}/positions/{p['id']}/close", {"orderType": "MARKET"})
        open_positions = [oldest]

    for p in open_positions:
        pid = str(p["id"])
        symbol = p["symbol"]
        side = str(p["side"]).upper()
        entry_price = float(p["entryPrice"])
        size = float(p.get("size", 0))
        current_sl = p.get("stopLoss")
        current_tp = p.get("takeProfit")
        unrealized_pnl = float(p.get("unrealizedPnL", 0) or p.get("pnl", 0) or 0)

        if size <= 0: continue

        sl_distance = MAX_LOSS_USD / size
        tp_distance = (MAX_LOSS_USD * REWARD_RATIO) / size
        lock_profit_distance = (MAX_LOSS_USD * LOCK_PROFIT_RATIO) / size

        if side == "LONG":
            target_sl = round(entry_price - sl_distance, 4)
            target_tp = round(entry_price + tp_distance, 4)
            profit_sl_price = round(entry_price + lock_profit_distance, 4)
        else:
            target_sl = round(entry_price + sl_distance, 4)
            target_tp = round(entry_price - tp_distance, 4)
            profit_sl_price = round(entry_price - lock_profit_distance, 4)

        key = pid
        needs_update = False
        payload = {"stopLossOrderType": "STOP_MARKET", "takeProfitOrderType": "TAKE_PROFIT_MARKET"}

        if key not in state:
            state[key] = {"symbol": symbol, "side": side, "entry": entry_price, "initial_sl": target_sl, "initial_tp": target_tp, "sl": target_sl, "tp": target_tp, "is_profit_locked": False}

        trigger_threshold = MAX_LOSS_USD * TRIGGER_RATIO
        if unrealized_pnl >= trigger_threshold and not state[key].get("is_profit_locked", False):
            print(f"🎯 قفل سود {symbol} روی {profit_sl_price}")
            state[key]["sl"] = profit_sl_price
            state[key]["is_profit_locked"] = True
            payload["stopLoss"] = str(profit_sl_price)
            needs_update = True

        if not needs_update:
            if current_sl is None or str(current_sl).strip() == "" or float(current_sl or 0) == 0:
                payload["stopLoss"] = str(state[key]["sl"])
                needs_update = True
            elif LOCK_STOP_LOSS:
                current_sl_f = float(current_sl)
                saved_sl_f = float(state[key]["sl"])
                risk_increased = (side == "LONG" and current_sl_f < saved_sl_f - 1e-6) or (side == "SHORT" and current_sl_f > saved_sl_f + 1e-6)
                if risk_increased:
                    payload["stopLoss"] = str(saved_sl_f)
                    needs_update = True
                elif abs(current_sl_f - saved_sl_f) > 1e-6:
                    state[key]["sl"] = current_sl_f

        target_tp_f = float(state[key]["tp"])
        if current_tp is None or str(current_tp).strip() == "" or float(current_tp or 0) == 0:
            payload["takeProfit"] = str(target_tp_f)
            needs_update = True
        else:
            current_tp_f = float(current_tp)
            too_far = (current_tp_f > target_tp_f + 1e-6) if side == "LONG" else (current_tp_f < target_tp_f - 1e-6)
            if too_far:
                payload["takeProfit"] = str(target_tp_f)
                needs_update = True

        if needs_update:
            if "stopLoss" not in payload: payload["stopLoss"] = str(current_sl) if current_sl else str(state[key]["sl"])
            if "takeProfit" not in payload: payload["takeProfit"] = str(current_tp) if current_tp else str(state[key]["tp"])
            result = request("PATCH", f"{FUTURES_PREFIX}/positions/{pid}/tpsl", payload)
            if result is not None: print(f"✅ TP/SL {symbol} آپدیت شد.")

    open_ids = {str(p["id"]) for p in open_positions}
    for k in list(state.keys()):
        if k not in open_ids: del state[k]
    save_state(state)

def main():
    start_time = time.time()
    print("🚀 چرخه RiskGuard شروع شد...")
    while True:
        if time.time() - start_time >= MAX_RUN_TIME_SECONDS:
            print("⏱ زمان چرخه تمام شد.")
            break
        try:
            state = load_state()
            run_riskguard_cycle(state)
        except Exception as e:
            print(f"خطا: {e}")
        time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
