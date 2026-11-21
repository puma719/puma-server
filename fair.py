"""Puma UP: честная игра (provably fair).

Этот файл публичный и самодостаточный (только стандартная библиотека). По нему любой может проверить результат
любой игры. Сервер использует те же функции, поэтому «как считает сервер» и «как проверяет игрок» не расходятся.

КАК ЭТО РАБОТАЕТ

1. Одиночные игры (Апгрейд, Кейсы, Мины, Башня, Горы).
   У каждого игрока есть пара сидов:
     server_seed  — секрет сервера. Игроку заранее показывают только его хэш commit = sha256(server_seed).
     client_seed  — задаёт игрок (можно поменять в любой момент).
   Каждая ставка получает номер nonce (0, 1, 2, ...). Все случайные числа ставки берутся из
     HMAC-SHA256(server_seed, "<client_seed>:<nonce>:<cursor>")
   и не зависят ни от суммы ставки, ни от ходов игрока. Сервер не может подменить результат после ставки:
   server_seed зафиксирован хэшем ещё до неё. Чтобы проверить, нажмите «Раскрыть и сменить сид»: сервер покажет
   прежний server_seed, и вы пересчитаете все свои ставки этим файлом (или кнопкой «Проверить» в приложении).

2. Общие игры (Ракета, Скачки пум).
   Исход раунда выбирается из цепочки хэшей: seed[i-1] = sha256(seed[i]), начало цепочки seed[0] (якорь)
   опубликовано заранее. Для раунда i в момент ставок известен commit = seed[i-1], а сам seed[i] показывают после
   раунда. Проверка: sha256(seed[i]) == commit, и результат раунда считается из seed[i] функциями ниже.
   Так сервер заранее обязан всю цепочку, а не выбирает seed под раунд.

ПРОВЕРКА ИЗ КОМАНДНОЙ СТРОКИ
  python3 fair.py bet --game upgrade --server-seed S --client-seed C --nonce 0 --params '{"chance": 0.5}'
  python3 fair.py round --game rocket --seed S --commit C
  python3 fair.py selftest
"""
import hashlib
import hmac
import json
import math
import secrets
import sys

VERSION = 1
CHAIN_LEN = 2_000_000   # раундов в одной цепочке (хватает больше чем на год)
CHAIN_STEP = 1000       # каждый 1000-й элемент хранится, остальные досчитываются на лету
RK_RTP = 90             # возврат игрокам в Ракете, %. Как RK_RTP в приложении.
HR_PICKS = 4            # пум на Скачках
GAMES = ("upgrade", "case", "mines", "tower", "peaks")


# ---------- примитивы ----------
def sha256_hex(text):
    return hashlib.sha256(text.encode()).hexdigest()


def new_server_seed():
    return secrets.token_hex(32)


def commit(server_seed):
    """Хэш, который показывают игроку до ставок."""
    return sha256_hex(server_seed)


def floats(server_seed, client_seed, nonce, count):
    """count чисел в [0, 1). Каждый HMAC даёт 8 чисел по 4 байта: b0/256 + b1/256^2 + b2/256^3 + b3/256^4."""
    out, cursor = [], 0
    while len(out) < count:
        d = hmac.new(server_seed.encode(), f"{client_seed}:{nonce}:{cursor}".encode(), hashlib.sha256).digest()
        for j in range(8):
            b = d[j * 4:j * 4 + 4]
            out.append(b[0] / 256 + b[1] / 65536 + b[2] / 16777216 + b[3] / 4294967296)
        cursor += 1
    return out[:count]


def seed_float(seed, label):
    """Число в [0, 1) из общего seed раунда: первые 52 бита HMAC-SHA256(seed, label)."""
    h = hmac.new(seed.encode(), label.encode(), hashlib.sha256).hexdigest()
    return int(h[:13], 16) / 16 ** 13


# ---------- одиночные игры: проверка параметров и результат по числам f ----------
def _num(v, lo, hi):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not (lo <= v <= hi) or v != v:
        raise ValueError("bad number")
    return v


def _int(v, lo, hi):
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ValueError("bad integer")
    return v


def clean_params(game, p):
    """Проверяет и нормализует параметры игры, иначе ValueError. Возвращает словарь, который и пишется в журнал."""
    if game not in GAMES or not isinstance(p, dict):
        raise ValueError("bad game")
    if game == "upgrade":
        return {"chance": _num(p.get("chance"), 0.001, 0.95)}
    if game == "case":
        w = p.get("weights")
        if not isinstance(w, list) or not 1 <= len(w) <= 64:
            raise ValueError("bad weights")
        return {"weights": [_num(x, 1e-15, 1e9) for x in w]}
    if game == "mines":
        return {"count": _int(p.get("count"), 1, 24)}
    if game == "tower":
        return {"cells": _int(p.get("cells"), 2, 6), "floors": _int(p.get("floors"), 1, 20)}
    c = p.get("chances")  # peaks
    if not isinstance(c, list) or not 1 <= len(c) <= 16:
        raise ValueError("bad chances")
    return {"chances": [_num(x, 0.01, 0.999) for x in c]}


def outcome(game, params, f):
    """Результат игры по числам f (их достаточно 25 штук). params уже прошли clean_params."""
    if game == "upgrade":
        return {"r": f[0], "win": f[0] < params["chance"]}
    if game == "case":
        total = 0.0
        for w in params["weights"]:  # обычное сложение слева направо (встроенный sum() в новых Python суммирует иначе)
            total += w
        r = f[0] * total
        for i, w in enumerate(params["weights"]):
            r -= w
            if r < 0:
                return {"index": i}
        return {"index": 0}
    if game == "mines":
        cells = list(range(25))
        for i in range(params["count"]):  # частичная перестановка Фишера-Йетса
            j = i + int(f[i] * (25 - i))
            cells[i], cells[j] = cells[j], cells[i]
        return {"mines": sorted(cells[:params["count"]])}
    if game == "tower":
        return {"traps": [int(f[i] * params["cells"]) for i in range(params["floors"])]}
    return {"rolls": f[:len(params["chances"])]}  # peaks: прыжок k удался, если rolls[k] < chances[k]


def bet_result(game, params, server_seed, client_seed, nonce):
    """Полный пересчёт одной ставки. Возвращает (нормализованные параметры, результат)."""
    p = clean_params(game, params)
    return p, outcome(game, p, floats(server_seed, client_seed, nonce, 25))


# ---------- общие игры ----------
def rocket_point(seed):
    """Точка взрыва Ракеты (множитель). Формула та же, что была раньше: возврат RK_RTP %."""
    r = seed_float(seed, "rocket")
    return min(500.0, max(1.0, math.floor(RK_RTP / (1 - r)) / 100))


def race_winner(seed):
    return int(seed_float(seed, "race:win") * HR_PICKS)


def race_anim_seed(seed):
    """Число для анимации забега. На исход не влияет."""
    return int(hmac.new(seed.encode(), b"race:seed", hashlib.sha256).hexdigest()[:8], 16) % 2 ** 31


def round_result(game, seed):
    if game == "rocket":
        return {"x": round(rocket_point(seed), 2)}
    if game == "race":
        return {"x": race_winner(seed)}
    raise ValueError("bad game")


# ---------- цепочка хэшей для общих игр ----------
class HashChain:
    """seed[N] — секретный корень, seed[k-1] = sha256(seed[k]); якорь seed[0] публикуется заранее.
    Раунды берут seed[1], seed[2], ... по порядку. Хранится каждый CHAIN_STEP-й элемент, остальное досчитывается."""

    def __init__(self, root_hex, n=CHAIN_LEN, step=CHAIN_STEP):
        if n % step:
            raise ValueError("n must be a multiple of step")
        self.n, self.step = n, step
        cur = bytes.fromhex(root_hex)
        self.cp = {n: cur}
        for k in range(n, 0, -1):
            cur = hashlib.sha256(cur.hex().encode()).digest()
            if (k - 1) % step == 0:
                self.cp[k - 1] = cur
        self.anchor = self.cp[0].hex()

    def seed(self, i):
        if not 0 <= i <= self.n:
            raise IndexError("chain exhausted")
        c = min(self.n, ((i + self.step - 1) // self.step) * self.step)
        cur = self.cp[c]
        for _ in range(c - i):
            cur = hashlib.sha256(cur.hex().encode()).digest()
        return cur.hex()


def new_chain_root():
    return secrets.token_hex(32)


# ---------- командная строка ----------
def _selftest():
    s, c = "a" * 64, "player"
    f = floats(s, c, 0, 25)
    assert all(0 <= x < 1 for x in f) and f == floats(s, c, 0, 25) and f != floats(s, c, 1, 25)
    assert commit(s) == hashlib.sha256(s.encode()).hexdigest()
    _, r = bet_result("mines", {"count": 3}, s, c, 5)
    assert len(r["mines"]) == 3 == len(set(r["mines"])) and all(0 <= m < 25 for m in r["mines"])
    _, r = bet_result("tower", {"cells": 3, "floors": 8}, s, c, 5)
    assert len(r["traps"]) == 8 and all(0 <= t < 3 for t in r["traps"])
    _, r = bet_result("case", {"weights": [80, 15, 4.7, 0.3]}, s, c, 5)
    assert 0 <= r["index"] < 4
    ch = HashChain("b" * 64, n=5000, step=1000)
    for i in (1, 2, 999, 1000, 1001, 4999, 5000):
        assert hashlib.sha256(ch.seed(i).encode()).hexdigest() == ch.seed(i - 1)
    assert 1.0 <= rocket_point(ch.seed(1)) <= 500.0 and 0 <= race_winner(ch.seed(1)) < 4
    wins = sum(outcome("upgrade", {"chance": 0.25}, floats(s, c, n, 1))["win"] for n in range(4000))
    assert 850 < wins < 1150, wins  # около 25 %
    print("selftest ok")


def _arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "selftest":
        return _selftest()
    if cmd == "bet":
        p, r = bet_result(_arg("--game"), json.loads(_arg("--params", "{}")), _arg("--server-seed"), _arg("--client-seed"), int(_arg("--nonce", "0")))
        print(json.dumps({"commit": commit(_arg("--server-seed")), "params": p, "result": r}, ensure_ascii=False, indent=2))
        return
    if cmd == "round":
        seed = _arg("--seed")
        ok = _arg("--commit") is None or commit(seed) == _arg("--commit")
        print(json.dumps({"hash_matches_commit": ok, "result": round_result(_arg("--game"), seed)}, indent=2))
        return
    print(__doc__)


if __name__ == "__main__":
    main()
