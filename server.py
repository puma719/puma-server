"""Puma UP — сервер бота: приём оплаты Telegram Stars (XTR).
Только стандартная библиотека Python 3.9+. Запуск:  BOT_TOKEN=... python3 server.py
"""
import hashlib, hmac, json, os, re, sqlite3, threading, time, urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip().strip('"').strip("'")
APP_URL = os.environ.get("APP_URL", "https://puma719.github.io/puma-app/")
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "https://puma719.github.io")
PORT = int(os.environ.get("PORT", "8080"))
DB_PATH = os.environ.get("DB_PATH", "puma.db")
MIN_STARS, MAX_STARS = 1, 100000
# Канал, подписка на который нужна для бесплатного кейса. Бот должен быть АДМИНИСТРАТОРОМ этого канала,
# иначе Telegram не отдаёт список подписчиков и проверка не сработает.
SUB_CHANNEL = os.environ.get("SUB_CHANNEL", "@pumaup").strip()

_db_lock = threading.Lock()
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS payments(charge_id TEXT PRIMARY KEY, user_id INTEGER, amount INTEGER, ts INTEGER);
CREATE TABLE IF NOT EXISTS credits(user_id INTEGER PRIMARY KEY, pending INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS promo_codes(code TEXT PRIMARY KEY, min_dep INTEGER NOT NULL DEFAULT 0,
                                       reward INTEGER NOT NULL, max_uses INTEGER NOT NULL, uses INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS promo_used(user_id INTEGER, code TEXT, ts INTEGER, PRIMARY KEY(user_id, code));
CREATE TABLE IF NOT EXISTS raffle_joins(raffle_id TEXT, cycle INTEGER, user_id INTEGER, ts INTEGER, PRIMARY KEY(raffle_id, cycle, user_id));
CREATE TABLE IF NOT EXISTS nft_deposits(dep_key TEXT PRIMARY KEY, user_id INTEGER, name TEXT, base_name TEXT, num INTEGER, model TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS feed(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, name TEXT, item TEXT, ts INTEGER);
""")
db.commit()


def seed_promos():
    """Промокоды берутся из переменной PROMOS (в коде их нет, репозиторий публичный).
    Формат: КОД:мин_пополнение:награда:сколько_активаций, через запятую.
    Пример: FREE5:0:10:5,DEP100:100:20:5,DEP1000:1000:100:5
    Счётчик использований при перезапуске не сбрасывается (если база лежит на постоянном томе)."""
    for item in os.environ.get("PROMOS", "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            code, min_dep, reward, max_uses = item.split(":")
            row = (code.strip().upper(), int(min_dep), int(reward), int(max_uses))
        except Exception:
            print("PROMOS: пропущен неверный элемент:", item[:40], flush=True)
            continue
        with _db_lock:
            db.execute("INSERT INTO promo_codes(code,min_dep,reward,max_uses) VALUES(?,?,?,?) "
                       "ON CONFLICT(code) DO UPDATE SET min_dep=excluded.min_dep, reward=excluded.reward, max_uses=excluded.max_uses", row)
            db.commit()
        print("PROMO OK:", row[0], flush=True)


_promo_fails = {}

# ---------- Розыгрыши: реальные участники ----------
# id: (минимальное пополнение Stars, длительность круга в секундах). Должно совпадать с RAFFLES в приложении.
RAFFLES = {"r1": (0, 3 * 3600), "r2": (100, 26 * 3600), "r3": (500, 72 * 3600)}


def raffles(user_id, join=None):
    """Участие в текущем круге розыгрыша. Круги общие для всех: номер круга = время // длительность."""
    now = time.time()
    err = ""
    with _db_lock:
        if join is not None:
            if join not in RAFFLES:
                err = "Такого розыгрыша нет"
            else:
                req, cyc = RAFFLES[join]
                dep = db.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE user_id=?", (user_id,)).fetchone()[0]
                if dep < req:
                    err = f"Нужно пополнить минимум на {req} ⭐ (у вас {dep})"
                else:
                    db.execute("INSERT OR IGNORE INTO raffle_joins VALUES(?,?,?,?)", (join, int(now // cyc), user_id, int(now)))
                    db.commit()
        out = {}
        for rid, (req, cyc) in RAFFLES.items():
            c = int(now // cyc)
            n = db.execute("SELECT COUNT(*) FROM raffle_joins WHERE raffle_id=? AND cycle=?", (rid, c)).fetchone()[0]
            j = db.execute("SELECT 1 FROM raffle_joins WHERE raffle_id=? AND cycle=? AND user_id=?", (rid, c, user_id)).fetchone()
            out[rid] = {"n": n, "joined": j is not None}
    return out, err


# ---------- Сколько игроков сейчас в каждой игре ----------
GAME_IDS = ("upgrade", "mines", "rocket", "tower", "race", "peaks")
_presence = {}


def online(user_id, game=None):
    """Игрок считается «играющим», если за последние 40 секунд сообщил, что находится в игре."""
    now = time.time()
    with _db_lock:
        for d in _presence.values():
            d.pop(user_id, None)
        if game in GAME_IDS:
            _presence.setdefault(game, {})[user_id] = now
        counts = {}
        for g in GAME_IDS:
            d = _presence.setdefault(g, {})
            for u in [u for u, t in d.items() if now - t > 40]:
                del d[u]
            counts[g] = len(d)
    return counts


# ---------- Лента LIVE: реальные выигрыши игроков ----------
_feed_last = {}
ITEM_RE = re.compile(r"^[a-z0-9_]{1,40}$")


def user_display(init_data):
    """Имя игрока берём из подписанных данных Telegram, а не от клиента."""
    try:
        u = json.loads(dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))["user"])
        name = "@" + u["username"] if u.get("username") else (u.get("first_name") or "Игрок")
        return str(name)[:32]
    except Exception:
        return "Игрок"


def feed(user_id, name, item=None):
    """Записывает выигрыш (не чаще раза в 3 секунды на игрока) и возвращает последние 14."""
    now = time.time()
    with _db_lock:
        if item and ITEM_RE.match(str(item)) and now - _feed_last.get(user_id, 0) >= 3:
            _feed_last[user_id] = now
            db.execute("INSERT INTO feed(user_id,name,item,ts) VALUES(?,?,?,?)", (user_id, name, item, int(now)))
            db.execute("DELETE FROM feed WHERE id NOT IN (SELECT id FROM feed ORDER BY id DESC LIMIT 200)")
            db.commit()
        rows = db.execute("SELECT user_id,name,item FROM feed ORDER BY id DESC LIMIT 14").fetchall()
    return [{"n": n, "item": it, "me": uid == user_id} for uid, n, it in rows]


def redeem_promo(user_id, code):
    """Возвращает (награда, ошибка). Всё под одной блокировкой, чтобы лимит не обошли параллельными запросами."""
    now = time.time()
    fails = [t for t in _promo_fails.get(user_id, []) if now - t < 3600]
    if len(fails) >= 10:
        return 0, "Слишком много попыток, попробуйте позже"

    def fail(msg, count=True):
        if count:
            fails.append(now)
            _promo_fails[user_id] = fails
        return 0, msg

    code = str(code or "").strip().upper()[:40]
    if not code:
        return fail("Введите код", False)
    with _db_lock:
        row = db.execute("SELECT min_dep,reward,max_uses,uses FROM promo_codes WHERE code=?", (code,)).fetchone()
        if not row:
            return fail("Такого кода нет")
        min_dep, reward, max_uses, uses = row
        if db.execute("SELECT 1 FROM promo_used WHERE user_id=? AND code=?", (user_id, code)).fetchone():
            return fail("Код уже использован вами", False)
        if uses >= max_uses:
            return fail("Код уже активировали максимальное число раз", False)
        if min_dep:
            dep = db.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE user_id=?", (user_id,)).fetchone()[0]
            if dep < min_dep:
                return fail(f"Нужно пополнить минимум на {min_dep} ⭐ (у вас {dep})", False)
        db.execute("INSERT INTO promo_used VALUES(?,?,?)", (user_id, code, int(now)))
        db.execute("UPDATE promo_codes SET uses=uses+1 WHERE code=?", (code,))
        db.commit()
        return reward, ""


def tg_api(method, payload=None, timeout=30):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ---------- NFT: приём подарков через бизнес-аккаунт ----------
# Игрок отправляет NFT-подарок на Telegram-аккаунт, подключённый к боту (Настройки → Telegram для бизнеса → Чат-боты).
# Telegram присылает боту service-сообщение unique_gift, и бот сразу записывает NFT на профиль отправителя.
def kv_set(k, v):
    with _db_lock:
        db.execute("INSERT INTO kv VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
        db.commit()


def kv_get(k, default=""):
    with _db_lock:
        row = db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


def deposit_account():
    """@username аккаунта, на который нужно отправлять NFT (из NFT_ACCOUNT или из подключения бизнес-аккаунта)."""
    return os.environ.get("NFT_ACCOUNT", "").strip() or kv_get("biz_username")


def nft_list(user_id):
    with _db_lock:
        rows = db.execute("SELECT name,base_name,num,model,ts FROM nft_deposits WHERE user_id=? ORDER BY ts DESC LIMIT 100",
                          (user_id,)).fetchall()
    return [{"name": n, "base": b, "num": k, "model": m, "ts": t} for n, b, k, m, t in rows]


def handle_business_connection(bc):
    """Запоминаем аккаунт, к которому подключён бот: именно туда игроки отправляют NFT."""
    u = bc.get("user") or {}
    if bc.get("is_enabled", True) and u.get("username"):
        kv_set("biz_username", "@" + u["username"])
        kv_set("biz_owner_id", u.get("id", 0))
    print("BUSINESS CONNECTION:", u.get("username") or u.get("id"), "enabled:", bc.get("is_enabled"), flush=True)


def handle_business_message(m):
    """Входящий NFT: зачисляется отправителю подарка. Свои подарки владельца аккаунта и обычные (не NFT) подарки не считаются."""
    ug = m.get("unique_gift")
    if not ug:
        return
    sender = (m.get("from") or {}).get("id")
    chat_id = (m.get("chat") or {}).get("id")
    # подарок получен от собеседника, а не отправлен им самим владельцем аккаунта
    if not sender or sender != chat_id or str(sender) == str(kv_get("biz_owner_id")):
        return
    g = ug.get("gift") or {}
    key = ug.get("owned_gift_id") or f"{m.get('business_connection_id')}:{chat_id}:{m.get('message_id')}"
    name = str(g.get("name") or g.get("base_name") or "NFT")[:80]
    base = str(g.get("base_name") or name)[:80]
    num = int(g.get("number") or 0)
    model = str((g.get("model") or {}).get("name") or "")[:80]
    with _db_lock:
        try:
            db.execute("INSERT INTO nft_deposits VALUES(?,?,?,?,?,?,?)", (key, sender, name, base, num, model, int(time.time())))
            db.commit()
        except sqlite3.IntegrityError:
            return  # этот подарок уже зачислен
    print("NFT DEPOSIT:", sender, name, num, flush=True)
    try:
        tg_api("sendMessage", {"chat_id": sender, "text": f"🎁 NFT получен: {base} #{num}. Он уже в вашем профиле в приложении.",
                               "reply_markup": {"inline_keyboard": [[{"text": "Открыть", "web_app": {"url": APP_URL}}]]}})
    except Exception as e:
        print("nft notify error:", repr(e), flush=True)


_sub_ok = {}  # user_id -> время последней успешной проверки (кэш 60 с, чтобы не долбить Telegram)


def is_subscribed(user_id):
    """Подписан ли игрок на SUB_CHANNEL. Возвращает (подписан, текст ошибки).
    Ошибка пустая, если Telegram ответил и игрок просто не подписан. При сбое проверки доступ НЕ даём."""
    now = time.time()
    if now - _sub_ok.get(user_id, 0) < 60:
        return True, ""
    try:
        res = tg_api("getChatMember", {"chat_id": SUB_CHANNEL, "user_id": user_id}, timeout=10)
    except urllib.error.HTTPError as e:
        try:
            desc = json.loads(e.read()).get("description", "")
        except Exception:
            desc = ""
        print("SUB CHECK ERROR:", e.code, desc, flush=True)
        if "user not found" in desc.lower() or "participant_id_invalid" in desc.lower():
            return False, ""  # Telegram не знает такого участника — считаем неподписанным
        return False, "Не удалось проверить подписку, попробуйте позже"
    except Exception as e:
        print("SUB CHECK ERROR:", repr(e), flush=True)
        return False, "Не удалось проверить подписку, попробуйте позже"
    r = res.get("result") or {}
    st = r.get("status")
    ok = st in ("creator", "administrator", "member") or (st == "restricted" and bool(r.get("is_member")))
    if ok:
        _sub_ok[user_id] = now
    return ok, ""


def check_init_data(init_data, max_age=86400):
    """Проверка подписи Telegram. Возвращает (user_id, причина): при успехе (id, ""), при отказе (None, текст)."""
    def fail(msg, detail=""):
        print("AUTH FAIL:", detail or msg, flush=True)
        return None, msg
    if not init_data:
        return fail("initData пустая", "initData пустая (приложение открыто не из Telegram или не передало данные)")
    try:
        pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        got = pairs.pop("hash", "")
        data_check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            return fail("подпись не совпала (BOT_TOKEN не от этого бота?)",
                        "подпись не совпала. BOT_TOKEN неверный или приложение открыто через другого бота")
        age = time.time() - int(pairs.get("auth_date", "0"))
        if age > max_age:
            return fail(f"initData устарела ({int(age // 3600)} ч)",
                        f"initData устарела ({int(age // 3600)} ч). Нужно закрыть и заново открыть приложение")
        return int(json.loads(pairs["user"])["id"]), ""
    except Exception as e:
        return fail("не удалось разобрать initData", "не удалось разобрать initData: " + repr(e))


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
        user_id, reason = check_init_data(init_data)
        if not user_id:
            return self._send(401, {"error": "bad initData: " + reason})

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

        if self.path == "/api/online":
            return self._send(200, {"counts": online(user_id, body.get("game"))})

        if self.path == "/api/raffles":
            out, err = raffles(user_id, body.get("join"))
            return self._send(200, {"raffles": out, "error": err})

        if self.path == "/api/feed":
            return self._send(200, {"items": feed(user_id, user_display(init_data), body.get("item"))})

        if self.path == "/api/subscribed":
            ok, err = is_subscribed(user_id)
            return self._send(200, {"subscribed": ok, "channel": SUB_CHANNEL, "error": err})

        if self.path == "/api/nfts":
            return self._send(200, {"items": nft_list(user_id), "account": deposit_account()})

        if self.path == "/api/promo":
            reward, err = redeem_promo(user_id, body.get("code"))
            if reward:
                return self._send(200, {"reward": reward})
            return self._send(400, {"error": err})

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
    if u.get("business_connection"):
        handle_business_connection(u["business_connection"])
        return
    if u.get("business_message"):
        handle_business_message(u["business_message"])
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
                                        "allowed_updates": ["message", "pre_checkout_query", "business_connection", "business_message"]}, timeout=40)
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
    seed_promos()
    threading.Thread(target=poll_loop, daemon=True).start()
    try:
        me = tg_api("getMe")
        print("BOT OK:", me.get("result", {}).get("username"), flush=True)
    except Exception as e:
        print("BOT TOKEN CHECK FAILED:", repr(e), flush=True)
    print(f"Puma UP server on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
