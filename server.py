"""Puma UP — сервер бота: приём оплаты Telegram Stars (XTR).
Только стандартная библиотека Python 3.9+. Запуск:  BOT_TOKEN=... python3 server.py
"""
import hashlib, hmac, json, math, os, re, secrets, sqlite3, threading, time, urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import fair  # честная игра: тот же файл игроки используют для проверки (см. fair.py и GET /fair.py)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip().strip('"').strip("'")
APP_URL = os.environ.get("APP_URL", "https://puma719.github.io/puma-app/")
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "https://puma719.github.io")
PORT = int(os.environ.get("PORT", "8080"))
DB_PATH = os.environ.get("DB_PATH", "puma.db")
MIN_STARS, MAX_STARS = 1, 100000
# Канал, подписка на который нужна для бесплатного кейса. Бот должен быть АДМИНИСТРАТОРОМ этого канала,
# иначе Telegram не отдаёт список подписчиков и проверка не сработает.
SUB_CHANNEL = os.environ.get("SUB_CHANNEL", "@pumaup").strip()
# Принимаем и «pumaup», и «https://t.me/pumaup», и числовой id канала (-100...)
SUB_CHANNEL = re.sub(r"^(https?://)?t\.me/", "", SUB_CHANNEL).lstrip("@")
if re.fullmatch(r"-?\d+", SUB_CHANNEL):
    # id канала без минуса и «100» (например 1234567890) Telegram не принимает: нужен вид -1001234567890
    if not SUB_CHANNEL.startswith("-") and len(SUB_CHANNEL) >= 10:
        SUB_CHANNEL = "-100" + SUB_CHANNEL
else:
    SUB_CHANNEL = "@" + SUB_CHANNEL

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
CREATE TABLE IF NOT EXISTS mp_rounds(id INTEGER PRIMARY KEY AUTOINCREMENT, game TEXT, x REAL, ts INTEGER);
CREATE TABLE IF NOT EXISTS mp_bets(id INTEGER PRIMARY KEY AUTOINCREMENT, game TEXT, round INTEGER, user_id INTEGER, name TEXT,
                                   bet INTEGER, pick INTEGER, cash REAL, x REAL, ts INTEGER);
CREATE INDEX IF NOT EXISTS mp_bets_user ON mp_bets(user_id, game, id);
""")
db.commit()
# Честная игра. fair_seeds: пара сидов игрока (revealed=1 после раскрытия server_seed), fair_bets: журнал ставок для проверки,
# fair_chains: цепочки хэшей общих игр (root секретный, anchor публичный), fair_rounds: раунды Ракеты и Скачек с раскрытым seed.
db.executescript("""
CREATE TABLE IF NOT EXISTS fair_seeds(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, server_seed TEXT NOT NULL, commit_hash TEXT NOT NULL,
                                      client_seed TEXT NOT NULL, nonce INTEGER NOT NULL DEFAULT 0, revealed INTEGER NOT NULL DEFAULT 0, ts INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS fair_seeds_user ON fair_seeds(user_id, revealed, id);
CREATE TABLE IF NOT EXISTS fair_bets(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, seed_id INTEGER NOT NULL, nonce INTEGER NOT NULL,
                                     game TEXT NOT NULL, params TEXT NOT NULL, result TEXT NOT NULL, ts INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS fair_bets_seed ON fair_bets(seed_id, nonce);
CREATE TABLE IF NOT EXISTS fair_chains(id INTEGER PRIMARY KEY AUTOINCREMENT, game TEXT NOT NULL, anchor TEXT NOT NULL, root TEXT NOT NULL,
                                       n INTEGER NOT NULL, ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS fair_rounds(game TEXT NOT NULL, round INTEGER NOT NULL, chain_id INTEGER NOT NULL, idx INTEGER NOT NULL, seed TEXT NOT NULL,
                                       commit_hash TEXT NOT NULL, x REAL NOT NULL, ts INTEGER NOT NULL, PRIMARY KEY(game, round));
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


# ---------- Мультиплеер: общие раунды Ракеты и Скачек ----------
# Раунды ведёт сервер: у всех игроков один раунд, один множитель, одни и те же ставки.
# Состояние считается по часам при каждом запросе, отдельный поток не нужен.
MP_GAMES = ("rocket", "race")
MP_BET_MAX, MP_BANK_MAX = 15000, 15000      # как BET_MAX и BANK_MAX в приложении
RK_RTP, RK_RATE = fair.RK_RTP, 0.09         # возврат игрокам, % (задаётся в fair.py) и рост множителя exp(0.09 * t)
RK_WAIT, RK_CRASH = 5.0, 2.5                # приём ставок и показ взрыва, секунд
HR_WAIT, HR_RUN, HR_DONE, HR_MULT = 8.0, 7.0, 3.5, 3.8
_mp_lock = threading.RLock()
_mp = {g: {"round": 0, "phase": "wait", "t0": 0.0, "cp": 1.0, "win": 0, "seed": 0, "bets": {}, "prev": None, "hist": [],
           "chain_id": 0, "idx": 0, "fseed": "", "commit": ""}
       for g in MP_GAMES}
_mp_my = {}  # (игра, user_id) -> последние 10 ставок игрока, новые первыми


def player_name(init_data):
    """Имя для списка ставок: имя из Telegram без @username (из подписанных данных, не от клиента)."""
    try:
        u = json.loads(dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))["user"])
        return (str(u.get("first_name") or "").strip() or str(u.get("username") or "").strip() or "Игрок")[:24]
    except Exception:
        return "Игрок"


def mp_load_history():
    """После перезапуска сервера подтягиваем историю иксов и победителей из базы."""
    with _mp_lock:
        for g in MP_GAMES:
            with _db_lock:
                rows = db.execute("SELECT x FROM mp_rounds WHERE game=? ORDER BY id DESC LIMIT 15", (g,)).fetchall()
            _mp[g]["hist"] = [round(r[0], 2) if g == "rocket" else int(r[0]) for r in rows]


def _mp_new_round(g, now):
    """Исход раунда определяется seed из заранее опубликованной цепочки хэшей (см. fair.py): сервер не выбирает его под ставки.
    Игрокам во время ставок виден только commit (хэш seed), сам seed раскрывается после раунда."""
    s = _mp[g]
    cid, idx, seed, com = fair_next_round(g)
    s.update(round=max(s["round"] + 1, int(now * 1000)), phase="wait", t0=now, bets={}, chain_id=cid, idx=idx, fseed=seed, commit=com)
    if g == "rocket":
        s["cp"] = fair.rocket_point(seed)  # точка взрыва известна только серверу и уходит клиентам после взрыва
    else:
        s["win"], s["seed"] = fair.race_winner(seed), fair.race_anim_seed(seed)


def _mp_end(g, s):
    """Когда закончится текущая фаза раунда (время сервера, секунды)."""
    ph = s["phase"]
    if g == "rocket":
        if ph == "wait":
            return s["t0"] + RK_WAIT
        if ph == "fly":
            return s["t0"] + math.log(s["cp"]) / RK_RATE
        return s["t0"] + RK_CRASH
    return s["t0"] + (HR_WAIT if ph == "wait" else HR_RUN if ph == "run" else HR_DONE)


def _mp_my_list(g, uid):
    lst = _mp_my.get((g, uid))
    if lst is None:
        with _db_lock:
            rows = db.execute("SELECT bet,pick,cash,x,ts FROM mp_bets WHERE game=? AND user_id=? ORDER BY id DESC LIMIT 10",
                              (g, uid)).fetchall()
        lst = [{"bet": r[0], "pick": r[1], "cash": r[2], "x": round(r[3], 2) if g == "rocket" else int(r[3]), "ts": r[4]}
               for r in rows]
        if len(_mp_my) > 5000:
            _mp_my.clear()
        _mp_my[(g, uid)] = lst
    return lst


def _mp_pub(g, b, me):
    o = {"n": b["name"], "bet": b["bet"], "me": bool(me)}
    if g == "race":
        o["pick"] = b["pick"]
    else:
        o["cash"] = b["cash"]
    return o


def _mp_settle(g, s):
    """Раунд закончился: пишем результат и ставки в базу, обновляем историю."""
    x = round(s["cp"], 2) if g == "rocket" else s["win"]
    ts = int(s["t0"])
    bets = sorted(s["bets"].items(), key=lambda kv: -kv[1]["bet"])
    s["prev"] = {"round": s["round"], "x": x, "fseed": s["fseed"], "commit": s["commit"], "idx": s["idx"],
                 "bets": [dict(_mp_pub(g, b, False), u=uid) for uid, b in bets]}
    s["hist"].insert(0, x)
    del s["hist"][15:]
    for uid, b in bets:
        lst = _mp_my_list(g, uid)
        lst.insert(0, {"bet": b["bet"], "pick": b["pick"], "cash": b["cash"], "x": x, "ts": ts})
        del lst[10:]
    with _db_lock:
        db.execute("INSERT INTO mp_rounds(game,x,ts) VALUES(?,?,?)", (g, x, ts))
        db.execute("INSERT OR IGNORE INTO fair_rounds(game,round,chain_id,idx,seed,commit_hash,x,ts) VALUES(?,?,?,?,?,?,?,?)",
                   (g, s["round"], s["chain_id"], s["idx"], s["fseed"], s["commit"], float(x), ts))
        if bets:
            db.executemany("INSERT INTO mp_bets(game,round,user_id,name,bet,pick,cash,x,ts) VALUES(?,?,?,?,?,?,?,?,?)",
                           [(g, s["round"], uid, b["name"], b["bet"], b["pick"], b["cash"], x, ts) for uid, b in bets])
        db.execute("DELETE FROM mp_rounds WHERE game=? AND id NOT IN (SELECT id FROM mp_rounds WHERE game=? ORDER BY id DESC LIMIT 200)", (g, g))
        db.commit()


def _mp_advance(g, now):
    """Двигает раунд по часам: ставки, полёт/гонка, взрыв/финиш, новый раунд."""
    s = _mp[g]
    if not s["round"]:
        _mp_new_round(g, now)
        return
    for _ in range(8):
        end = _mp_end(g, s)
        if now < end:
            return
        ph = s["phase"]
        if ph == "wait":
            s.update(phase="fly" if g == "rocket" else "run", t0=end)
        elif ph in ("fly", "run"):
            s.update(phase="crash" if g == "rocket" else "done", t0=end)
            _mp_settle(g, s)
        else:
            _mp_new_round(g, end)
    if now >= _mp_end(g, s):  # сервер долго никто не опрашивал — начинаем заново с текущего момента
        _mp_new_round(g, now)


def mp_state(user_id, g):
    with _mp_lock:
        now = time.time()
        _mp_advance(g, now)
        s = _mp[g]
        ph = s["phase"]
        out = {"now": int(now * 1000), "game": g, "round": s["round"], "phase": ph, "t0": int(s["t0"] * 1000),
               "wait": int((RK_WAIT if g == "rocket" else HR_WAIT) * 1000), "hist": list(s["hist"]),
               "commit": s["commit"], "idx": s["idx"]}
        if ph == ("crash" if g == "rocket" else "done"):  # seed раунда раскрывается только после его окончания
            out["fseed"] = s["fseed"]
        if g == "rocket":
            if ph == "crash":
                out["cp"] = round(s["cp"], 2)
            if ph != "fly":  # момент взрыва во время полёта не раскрываем
                out["ends"] = int(_mp_end(g, s) * 1000)
        else:
            out["mult"] = HR_MULT
            if ph in ("run", "done"):  # победитель известен только после закрытия ставок
                out["win"], out["seed"] = s["win"], s["seed"]
            out["ends"] = int(_mp_end(g, s) * 1000)
        bets = sorted(s["bets"].items(), key=lambda kv: -kv[1]["bet"])
        out["bets"] = [_mp_pub(g, b, uid == user_id) for uid, b in bets]
        mine = s["bets"].get(user_id)
        out["mine"] = _mp_pub(g, mine, True) if mine else None
        prev = s["prev"]
        out["prev"] = None if not prev else {"round": prev["round"], "x": prev["x"], "fseed": prev["fseed"], "commit": prev["commit"], "idx": prev["idx"],
                                             "bets": [{k: v for k, v in b.items() if k != "u"} | {"me": b["u"] == user_id} for b in prev["bets"]]}
        out["my"] = [dict(r) for r in _mp_my_list(g, user_id)]
    out["online"] = online(user_id, g).get(g, 0)
    return out


def mp_bet(user_id, name, g, amount, pick):
    try:
        amount = int(amount)
        pick = int(pick or 0)
    except Exception:
        return {"error": "Неверная ставка"}
    if amount < 1:
        return {"error": "Введите ставку"}
    if amount > MP_BET_MAX:
        return {"error": f"Максимальная ставка: {MP_BET_MAX} ⭐"}
    if g == "race" and not 0 <= pick <= 3:
        return {"error": "Выберите пуму"}
    with _mp_lock:
        _mp_advance(g, time.time())
        s = _mp[g]
        if s["phase"] != "wait":
            return {"error": "Ставки закрыты, дождитесь следующего раунда"}
        if user_id in s["bets"]:
            return {"error": "Ставка уже сделана"}
        bank = sum(b["bet"] for b in s["bets"].values())
        if bank + amount > MP_BANK_MAX:
            left = max(0, MP_BANK_MAX - bank)
            return {"error": f"Банк раунда ограничен {MP_BANK_MAX} ⭐. Можно поставить до {left}" if left else f"Банк раунда заполнен ({MP_BANK_MAX} ⭐)"}
        s["bets"][user_id] = {"name": name, "bet": amount, "pick": pick if g == "race" else 0, "cash": None}
        return {"ok": True, "round": s["round"]}


def mp_cancel(user_id, g):
    with _mp_lock:
        _mp_advance(g, time.time())
        s = _mp[g]
        if user_id not in s["bets"]:
            return {"ok": True}
        if s["phase"] != "wait":
            return {"error": "Ставки уже закрыты"}
        del s["bets"][user_id]
        return {"ok": True}


def mp_cash(user_id, at):
    """Забрать выигрыш в Ракете. Множитель считает сервер по своим часам: «at» — это то, что игрок видел на экране
    (или его автовывод), оно принимается только если не больше реального множителя в этот момент."""
    with _mp_lock:
        now = time.time()
        _mp_advance("rocket", now)
        s = _mp["rocket"]
        b = s["bets"].get(user_id)
        if not b:
            return {"error": "Нет ставки в этом раунде"}
        if b["cash"] is not None:
            return {"ok": True, "m": b["cash"], "again": True}  # ответ потерялся — отдаём тот же результат
        if s["phase"] != "fly":
            return {"error": "late"}
        m = math.exp(RK_RATE * (now - s["t0"]))
        if m >= s["cp"]:
            return {"error": "late"}
        try:
            at = float(at or 0)
        except Exception:
            at = 0.0
        use = at if 1.0 <= at <= m else m
        b["cash"] = max(1.0, math.floor(use * 100) / 100)
        return {"ok": True, "m": b["cash"]}


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
    credit_nft(key, sender, g.get("name") or g.get("base_name"), g.get("base_name"), g.get("number"), (g.get("model") or {}).get("name"))


def credit_nft(key, user_id, name, base, num, model):
    """Записывает NFT на профиль игрока (один раз на ключ) и пишет ему в личку. Возвращает True, если зачислено."""
    name = str(name or base or "NFT")[:80]
    base = str(base or name)[:80]
    try:
        num = int(num or 0)
    except Exception:
        num = 0
    model = str(model or "")[:80]
    with _db_lock:
        try:
            db.execute("INSERT INTO nft_deposits VALUES(?,?,?,?,?,?,?)", (str(key)[:200], user_id, name, base, num, model, int(time.time())))
            db.commit()
        except sqlite3.IntegrityError:
            return False  # этот подарок уже зачислен
    print("NFT DEPOSIT:", user_id, name, num, flush=True)
    try:
        tg_api("sendMessage", {"chat_id": user_id, "text": f"🎁 NFT получен: {base} #{num}. Он уже в вашем профиле в приложении.",
                               "reply_markup": {"inline_keyboard": [[{"text": "Открыть", "web_app": {"url": APP_URL}}]]}})
    except Exception as e:
        print("nft notify error:", repr(e), flush=True)
    return True


def handle_credit_command(m):
    """Ручное зачисление NFT админом: /credit <user_id> <название> <номер>  (например: /credit 123456789 Plush Pepe 1234).
    Админы задаются переменной окружения ADMIN_IDS (Telegram ID через запятую; свой ID можно узнать командой /id)."""
    chat_id = m["chat"]["id"]
    uid = str((m.get("from") or {}).get("id"))
    admins = {x.strip() for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
    if uid not in admins:
        return  # молча игнорируем: обычным пользователям команда недоступна
    parts = str(m.get("text", "")).split()[1:]
    usage = "Формат: /credit <ID игрока> <название NFT> <номер>\nПример: /credit 123456789 Plush Pepe 1234"
    if len(parts) < 3 or not parts[0].lstrip("-").isdigit() or not parts[-1].isdigit():
        tg_api("sendMessage", {"chat_id": chat_id, "text": usage})
        return
    target, num, name = int(parts[0]), int(parts[-1]), " ".join(parts[1:-1])
    key = f"manual:{target}:{name}:{num}:{int(time.time())}"
    ok = credit_nft(key, target, name, name, num, "")
    tg_api("sendMessage", {"chat_id": chat_id, "text": f"✅ Зачислено: {name} #{num} → {target}" if ok else "⚠️ Не удалось зачислить"})
    print("MANUAL CREDIT by", uid, "->", target, name, num, flush=True)


_sub_ok = {}  # user_id -> время последней успешной проверки (кэш 60 с, чтобы не долбить Telegram)
# Состояние проверки подписки для диагностики: видно в логах и на /health (ok / bot_not_admin / error + причина).
SUB_DIAG = {"status": "unknown", "detail": ""}


def sub_diagnose():
    """Проверка при старте. Telegram отдаёт статус участников канала только боту-администратору этого канала.
    Если бот не админ или канал указан неверно, подписка у игроков не проверится: причина пишется в лог."""
    try:
        me = tg_api("getMe")["result"]["id"]
        res = tg_api("getChatMember", {"chat_id": SUB_CHANNEL, "user_id": me}, timeout=10)
        st = (res.get("result") or {}).get("status")
        if st in ("administrator", "creator"):
            SUB_DIAG.update(status="ok", detail="")
        else:
            SUB_DIAG.update(status="bot_not_admin", detail=f"бот в канале {SUB_CHANNEL} со статусом «{st}»")
    except urllib.error.HTTPError as e:
        try:
            desc = json.loads(e.read()).get("description", "")
        except Exception:
            desc = ""
        SUB_DIAG.update(status="error", detail=f"{e.code} {desc}".strip())
    except Exception as e:
        SUB_DIAG.update(status="error", detail=repr(e))
    if SUB_DIAG["status"] == "ok":
        print("SUB CHECK OK: бот администратор канала", SUB_CHANNEL, flush=True)
    else:
        print(f"SUB CHECK FAILED ({SUB_DIAG['status']}): {SUB_DIAG['detail']}. Добавьте бота АДМИНИСТРАТОРОМ в канал "
              f"{SUB_CHANNEL} (достаточно любых прав) и проверьте переменную SUB_CHANNEL.", flush=True)


def is_subscribed(user_id):
    """Подписан ли игрок на SUB_CHANNEL. Возвращает (подписан, текст ошибки).
    Ошибка пустая, если Telegram ответил и игрок просто не подписан. При сбое проверки доступ НЕ даём."""
    now = time.time()
    if now - _sub_ok.get(user_id, 0) < 60:
        return True, ""
    res = None
    for attempt in (1, 2):  # один повтор при временном сбое (таймаут, 429, 5xx)
        try:
            res = tg_api("getChatMember", {"chat_id": SUB_CHANNEL, "user_id": user_id}, timeout=10)
            break
        except urllib.error.HTTPError as e:
            try:
                desc = json.loads(e.read()).get("description", "")
            except Exception:
                desc = ""
            low = desc.lower()
            print("SUB CHECK ERROR:", e.code, desc, "| канал:", SUB_CHANNEL, flush=True)
            if "user not found" in low or "participant_id_invalid" in low:
                return False, ""  # Telegram не знает такого участника — считаем неподписанным
            if attempt == 1 and (e.code == 429 or e.code >= 500):
                time.sleep(1)
                continue
            if "chat not found" in low or "not enough rights" in low or "administrator" in low or "inaccessible" in low:
                SUB_DIAG.update(status="bot_not_admin", detail=f"{e.code} {desc}".strip())
                print(f"SUB CHECK CONFIG: бот должен быть АДМИНИСТРАТОРОМ канала {SUB_CHANNEL}; "
                      "проверьте SUB_CHANNEL (@username или -100…) и права бота", flush=True)
            else:
                SUB_DIAG.update(status="error", detail=f"{e.code} {desc}".strip())
            return False, "Не удалось проверить подписку, попробуйте позже"
        except Exception as e:
            print("SUB CHECK ERROR:", repr(e), flush=True)
            if attempt == 1:
                time.sleep(1)
                continue
            SUB_DIAG.update(status="error", detail=repr(e))
            return False, "Не удалось проверить подписку, попробуйте позже"
    r = res.get("result") or {}
    st = r.get("status")
    SUB_DIAG.update(status="ok", detail="")  # Telegram ответил — проверка работает
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
        url = urllib.parse.urlparse(self.path)
        if url.path == "/health":
            return self._send(200, {"ok": True, "sub": SUB_DIAG["status"], "sub_detail": SUB_DIAG["detail"]})
        # Публичные данные честной игры: якоря цепочек, раскрытые раунды и точный код проверки, который работает на сервере.
        if url.path == "/api/fair/info":
            return self._send(200, fair_info())
        if url.path == "/api/fair/rounds":
            g = (urllib.parse.parse_qs(url.query).get("game") or [""])[0]
            return self._send(200, {"rounds": fair_rounds(g)}) if g in MP_GAMES else self._send(400, {"error": "bad game"})
        if url.path == "/fair.py":
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fair.py"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad json"})
        if self.path == "/api/userbot/nft":
            # Приём NFT от юзербота (аккаунт без Premium). Защита: общий секрет USERBOT_SECRET в заголовке.
            secret = os.environ.get("USERBOT_SECRET", "")
            got = self.headers.get("X-Userbot-Secret", "")
            if not secret or not hmac.compare_digest(secret.encode(), got.encode()):
                return self._send(403, {"error": "forbidden"})
            try:
                uid = int(body["user_id"])
                credited = credit_nft(body["key"], uid, body.get("name"), body.get("base"), body.get("num"), body.get("model"))
            except Exception:
                return self._send(400, {"error": "bad payload"})
            if body.get("account"):
                kv_set("biz_username", str(body["account"])[:64])
            return self._send(200, {"credited": credited})

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

        if self.path.startswith("/api/mp/"):
            g = str(body.get("game") or "")
            if g not in MP_GAMES:
                return self._send(400, {"error": "bad game"})
            act = self.path[len("/api/mp/"):]
            if act == "state":
                return self._send(200, mp_state(user_id, g))
            if act == "bet":
                return self._send(200, mp_bet(user_id, player_name(init_data), g, body.get("amount"), body.get("pick")))
            if act == "cancel":
                return self._send(200, mp_cancel(user_id, g))
            if act == "cash" and g == "rocket":
                return self._send(200, mp_cash(user_id, body.get("at")))
            return self._send(404, {"error": "not found"})

        if self.path == "/api/fair/state":
            return self._send(200, fair_state(user_id))

        if self.path == "/api/fair/bet":
            res, err = fair_bet(user_id, str(body.get("game") or ""), body.get("params"))
            return self._send(200, res) if res else self._send(400, {"error": err})

        if self.path == "/api/fair/rotate":
            res, err = fair_rotate(user_id, body.get("client_seed"))
            return self._send(200, res) if res else self._send(400, {"error": err})

        if self.path == "/api/fair/bets":
            res = fair_seed_bets(user_id, body.get("seed_id"))
            return self._send(200, res) if res else self._send(404, {"error": "not found"})

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


# ---------- Честная игра (описание и проверка: fair.py) ----------
_chains = {}      # игра -> {"id": номер цепочки, "chain": HashChain, "idx": последний выданный раунд}
_fair_rate = {}   # user_id -> времена последних ставок (защита журнала от флуда)


def _chain_new(g):
    root = fair.new_chain_root()
    ch = fair.HashChain(root)
    with _db_lock:
        cur = db.execute("INSERT INTO fair_chains(game,anchor,root,n,ts) VALUES(?,?,?,?,?)", (g, ch.anchor, root, ch.n, int(time.time())))
        db.commit()
        cid = cur.lastrowid
    print("FAIR CHAIN:", g, "id", cid, "anchor", ch.anchor, flush=True)
    return {"id": cid, "chain": ch, "idx": 0}


def _chain_load(g):
    """Цепочка игры из базы (секретный корень лежит там же) или новая, если её ещё нет."""
    with _db_lock:
        row = db.execute("SELECT id,root,n FROM fair_chains WHERE game=? ORDER BY id DESC LIMIT 1", (g,)).fetchone()
    if row is None:
        return _chain_new(g)
    return {"id": row[0], "chain": fair.HashChain(row[1], row[2]), "idx": int(kv_get(f"chain_idx:{g}:{row[0]}", "0") or 0)}


def fair_init():
    for g in MP_GAMES:
        _chains[g] = _chain_load(g)
        print("FAIR ANCHOR:", g, _chains[g]["chain"].anchor, flush=True)


def fair_next_round(g):
    """Следующий раунд цепочки: (id цепочки, номер, seed, commit). Номер записывается до выдачи, поэтому seed не используется дважды.
    Если сервер перезапустился посреди раунда, его номер остаётся пропущенным: цепочка от этого не ломается."""
    c = _chains.get(g)
    if c is None:
        c = _chains[g] = _chain_load(g)
    if c["idx"] + 1 > c["chain"].n:  # цепочка кончилась: начинаем новую, её якорь тоже публичный
        c = _chains[g] = _chain_new(g)
    c["idx"] += 1
    kv_set(f"chain_idx:{g}:{c['id']}", c["idx"])
    ch = c["chain"]
    return c["id"], c["idx"], ch.seed(c["idx"]), ch.seed(c["idx"] - 1)


def _fair_create(user_id, client_seed):
    """Новая пара сидов игрока. Вызывать под _db_lock."""
    seed = fair.new_server_seed()
    cur = db.execute("INSERT INTO fair_seeds(user_id,server_seed,commit_hash,client_seed,nonce,revealed,ts) VALUES(?,?,?,?,0,0,?)",
                     (user_id, seed, fair.commit(seed), client_seed, int(time.time())))
    db.commit()
    return (cur.lastrowid, seed, fair.commit(seed), client_seed, 0)


def _fair_active(user_id):
    """Активная пара сидов (id, server_seed, commit, client_seed, nonce); создаётся при первом обращении. Вызывать под _db_lock."""
    row = db.execute("SELECT id,server_seed,commit_hash,client_seed,nonce FROM fair_seeds WHERE user_id=? AND revealed=0 ORDER BY id DESC LIMIT 1",
                     (user_id,)).fetchone()
    return row or _fair_create(user_id, secrets.token_hex(6))


def _fair_bets(seed_id, limit, newest_first=True):
    rows = db.execute(f"SELECT nonce,game,params,result,ts FROM fair_bets WHERE seed_id=? ORDER BY nonce {'DESC' if newest_first else 'ASC'} LIMIT ?",
                      (seed_id, limit)).fetchall()
    return [{"nonce": n, "game": g, "params": json.loads(p), "result": json.loads(r), "ts": t} for n, g, p, r, t in rows]


def fair_state(user_id):
    """Что видит игрок: хэш текущего server_seed, свой client_seed, номер следующей ставки, последние ставки и раскрытые сиды."""
    with _db_lock:
        sid, _, com, cs, nonce = _fair_active(user_id)
        bets = _fair_bets(sid, 30)
        old = db.execute("SELECT id,server_seed,commit_hash,client_seed,nonce,ts FROM fair_seeds WHERE user_id=? AND revealed=1 ORDER BY id DESC LIMIT 10",
                         (user_id,)).fetchall()
    return {"commit": com, "client_seed": cs, "nonce": nonce, "bets": bets,
            "revealed": [{"id": i, "server_seed": s, "commit": c, "client_seed": k, "bets_count": n, "ts": t} for i, s, c, k, n, t in old]}


def fair_bet(user_id, game, params):
    """Бросок для одной ставки. Результат считается из текущей пары сидов и номера ставки и пишется в журнал. Возвращает (ответ, ошибка)."""
    now = time.time()
    hits = [t for t in _fair_rate.get(user_id, []) if now - t < 10]
    if len(hits) >= 40:
        return None, "Слишком часто, подождите немного"
    hits.append(now)
    if len(_fair_rate) > 5000:
        _fair_rate.clear()
    _fair_rate[user_id] = hits
    try:
        p = fair.clean_params(game, params)
    except ValueError:
        return None, "Неверные параметры игры"
    with _db_lock:
        sid, seed, com, cs, nonce = _fair_active(user_id)
        res = fair.outcome(game, p, fair.floats(seed, cs, nonce, 25))
        db.execute("INSERT INTO fair_bets(user_id,seed_id,nonce,game,params,result,ts) VALUES(?,?,?,?,?,?,?)",
                   (user_id, sid, nonce, game, json.dumps(p), json.dumps(res), int(now)))
        db.execute("UPDATE fair_seeds SET nonce=nonce+1 WHERE id=?", (sid,))
        db.commit()
    return {"nonce": nonce, "commit": com, "client_seed": cs, "game": game, "params": p, "result": res}, ""


def fair_rotate(user_id, client_seed=None):
    """Раскрывает текущий server_seed (теперь по нему можно проверить все ставки) и начинает новую пару сидов.
    Новый client_seed необязателен. Возвращает (состояние + раскрытый сид со всеми его ставками, ошибка)."""
    cs = None
    if client_seed is not None:
        cs = str(client_seed).strip()
        if not 1 <= len(cs) <= 64 or any(ord(ch) < 32 for ch in cs):
            return None, "Клиентский сид: от 1 до 64 символов"
    with _db_lock:
        sid, seed, com, old_cs, nonce = _fair_active(user_id)
        db.execute("UPDATE fair_seeds SET revealed=1 WHERE id=?", (sid,))
        _fair_create(user_id, cs or old_cs)
        bets = _fair_bets(sid, 1000, newest_first=False)
    out = fair_state(user_id)
    out["reveal"] = {"id": sid, "server_seed": seed, "commit": com, "client_seed": old_cs, "bets_count": nonce, "bets": bets}
    return out, ""


def fair_seed_bets(user_id, seed_id):
    """Ставки уже раскрытого сида (для проверки). Чужие и ещё не раскрытые сиды не отдаются."""
    try:
        seed_id = int(seed_id)
    except Exception:
        return None
    with _db_lock:
        row = db.execute("SELECT server_seed,commit_hash,client_seed,nonce FROM fair_seeds WHERE id=? AND user_id=? AND revealed=1",
                         (seed_id, user_id)).fetchone()
        bets = _fair_bets(seed_id, 1000, newest_first=False) if row else []
    if not row:
        return None
    return {"id": seed_id, "server_seed": row[0], "commit": row[1], "client_seed": row[2], "bets_count": row[3], "bets": bets}


def fair_info():
    with _db_lock:
        rows = db.execute("SELECT id,game,anchor,n,ts FROM fair_chains ORDER BY id").fetchall()
    return {"version": fair.VERSION, "rocket_rtp": fair.RK_RTP, "source": "/fair.py",
            "chains": [{"id": i, "game": g, "anchor": a, "length": n, "ts": t} for i, g, a, n, t in rows]}


def fair_rounds(game, limit=50):
    with _db_lock:
        rows = db.execute("SELECT round,chain_id,idx,seed,commit_hash,x,ts FROM fair_rounds WHERE game=? ORDER BY ts DESC, round DESC LIMIT ?",
                          (game, max(1, min(int(limit), 200)))).fetchall()
    return [{"round": r, "chain": c, "idx": i, "seed": s, "commit": k, "x": round(x, 2) if game == "rocket" else int(x), "ts": t}
            for r, c, i, s, k, x, t in rows]


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
    text = str(m.get("text", ""))
    if text.startswith("/id"):
        tg_api("sendMessage", {"chat_id": m["chat"]["id"], "text": f"Ваш ID: {(m.get('from') or {}).get('id')}"})
        return
    if text.startswith("/credit"):
        handle_credit_command(m)
        return
    if text.startswith("/start"):
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
    mp_load_history()
    fair_init()
    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=sub_diagnose, daemon=True).start()
    try:
        me = tg_api("getMe")
        print("BOT OK:", me.get("result", {}).get("username"), flush=True)
    except Exception as e:
        print("BOT TOKEN CHECK FAILED:", repr(e), flush=True)
    print(f"Puma UP server on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
