"""Puma UP — сервер бота: приём оплаты Telegram Stars (XTR).
Только стандартная библиотека Python 3.9+. Запуск:  BOT_TOKEN=... python3 server.py
"""
import hashlib, hmac, json, os, sqlite3, threading, time, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
APP_URL = os.environ.get("APP_URL", "https://puma719.github.io/puma-app/")
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "https://puma719.github.io")
PORT = int(os.environ.get("PORT", "8080"))
DB_PATH = os.environ.get("DB_PATH", "puma.db")
MIN_STARS, MAX_STARS = 1, 100000

_db_lock = threading.Lock()
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS payments(charge_id TEXT PRIMARY KEY, user_id INTEGER, amount INTEGER, ts INTEGER);
CREATE TABLE IF NOT EXISTS credits(user_id INTEGER PRIMARY KEY, pending INTEGER NOT NULL DEFAULT 0);
""")
db.commit()


def tg_api(method, payload=None, timeout=30):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def check_init_data(init_data, max_age=86400):
    """Проверка подписи Telegram. Возвращает user_id или None."""
    try:
        pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        got = pairs.pop("hash", "")
        data_check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > max_age:
            return None
        return int(json.loads(pairs["user"])["id"])
    except Exception:
        return None


def add_payment(charge_id, user_id, amount):
    """Начисляет звёзды один раз на каждый платёж (защита от повторов)."""
    with _db_lock:
        try:
            db.execute("INSERT INTO payments VALUES(?,?,?,?)", (charge_id, user_id, amount, int(time.time())))
        except sqlite3.IntegrityError:
            return False
        db.execute(
            "INSERT INTO credits(user_id,pending) VALUES(?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET pending=pending+excluded.pending", (user_id, amount))
        db.commit()
        return True


def claim(user_id):
    with _db_lock:
        row = db.execute("SELECT pending FROM credits WHERE user_id=?", (user_id,)).fetchone()
        n = row[0] if row else 0
        if n:
            db.execute("UPDATE credits SET pending=0 WHERE user_id=?", (user_id,))
            db.commit()
        return n


# ---------- HTTP API для приложения ----------
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Init-Data")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        # 204 обязан быть БЕЗ тела, иначе iOS (WebKit) отбрасывает preflight
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Init-Data")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self._send(200, {"ok": True}) if self.path == "/health" else self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad json"})
        init_data = body.get("initData") or self.headers.get("X-Init-Data", "")
        user_id = check_init_data(init_data)
        if not user_id:
            print("AUTH FAIL: initData не прошла проверку. Скорее всего BOT_TOKEN неверный или от другого бота", flush=True)
            return self._send(401, {"error": "bad initData"})

        if self.path == "/api/invoice":
            try:
                amount = int(body.get("amount"))
            except Exception:
                return self._send(400, {"error": "bad amount"})
            if not MIN_STARS <= amount <= MAX_STARS:
                return self._send(400, {"error": "amount out of range"})
            try:
                res = tg_api("createInvoiceLink", {
                    "title": "Пополнение Puma UP",
                    "description": f"Пополнение баланса на {amount} ⭐",
                    "payload": f"dep:{user_id}:{amount}",
                    "currency": "XTR",
                    "prices": [{"label": "Звёзды", "amount": amount}],
                })
                if res.get("ok"):
                    return self._send(200, {"link": res["result"]})
                print("TELEGRAM ERROR:", res, flush=True)
                return self._send(502, {"error": "telegram"})
            except Exception as e:
                print("INVOICE ERROR:", repr(e), flush=True)
                return self._send(502, {"error": "telegram"})

        if self.path == "/api/claim":
            return self._send(200, {"credited": claim(user_id)})

        self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


# ---------- Бот: long polling ----------
def handle_update(u):
    pcq = u.get("pre_checkout_query")
    if pcq:
        ok = pcq.get("currency") == "XTR" and str(pcq.get("invoice_payload", "")).startswith(f"dep:{pcq['from']['id']}:")
        tg_api("answerPreCheckoutQuery", {"pre_checkout_query_id": pcq["id"], "ok": ok,
                                          **({} if ok else {"error_message": "Платёж отклонён"})})
        return
    m = u.get("message") or {}
    sp = m.get("successful_payment")
    if sp:
        user_id = m["from"]["id"]
        if sp.get("currency") == "XTR" and str(sp.get("invoice_payload", "")).startswith(f"dep:{user_id}:"):
            if add_payment(sp["telegram_payment_charge_id"], user_id, int(sp["total_amount"])):
                tg_api("sendMessage", {"chat_id": m["chat"]["id"],
                                       "text": f"✅ Оплата получена: +{sp['total_amount']} ⭐. Откройте приложение — баланс обновится."})
        return
    if str(m.get("text", "")).startswith("/start"):
        tg_api("sendMessage", {"chat_id": m["chat"]["id"], "text": "Добро пожаловать в Puma UP!",
                               "reply_markup": {"inline_keyboard": [[{"text": "Открыть", "web_app": {"url": APP_URL}}]]}})


def poll_loop():
    offset = 0
    while True:
        try:
            res = tg_api("getUpdates", {"offset": offset, "timeout": 25,
                                        "allowed_updates": ["message", "pre_checkout_query"]}, timeout=40)
            for u in res.get("result", []):
                offset = u["update_id"] + 1
                try:
                    handle_update(u)
                except Exception as e:
                    print("update error:", e)
        except Exception as e:
            print("poll error:", e)
            time.sleep(3)


if __name__ == "__main__":
    if not BOT_TOKEN:
        raise SystemExit("Задайте переменную BOT_TOKEN (токен от @BotFather)")
    threading.Thread(target=poll_loop, daemon=True).start()
    BOT_TOKEN = BOT_TOKEN.strip().strip('"').strip("'")
    try:
        me = tg_api("getMe")
        print("BOT OK:", me.get("result", {}).get("username"), flush=True)
    except Exception as e:
        print("BOT TOKEN CHECK FAILED:", repr(e), flush=True)
    print(f"Puma UP server on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
