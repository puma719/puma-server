"""Юзербот-приёмник NFT: сидит в обычном Telegram-аккаунте (Premium не нужен), видит входящие NFT-подарки
и сообщает серверу, чтобы NFT сразу появился в профиле отправителя.

ВНИМАНИЕ: юзерботы не одобрены Telegram, аккаунт могут ограничить или заблокировать, а вместе с ним пропадут NFT.
Используйте отдельный аккаунт и не храните на нём ничего ценного, кроме принятых подарков.
Строка сессии (TG_SESSION) даёт ПОЛНЫЙ доступ к аккаунту: никогда не публикуйте её и не коммитьте в git.

Установка:  pip install telethon
Ключи api_id и api_hash: https://my.telegram.org -> API development tools.

1) Один раз получить строку сессии (на своём компьютере):
     TG_API_ID=... TG_API_HASH=... python3 userbot.py --login
   Введите номер, код из Telegram (и пароль 2FA, если есть). Скрипт напечатает длинную строку сессии.

2) Запуск (на сервере/в Railway как отдельный сервис):
     TG_API_ID=... TG_API_HASH=... TG_SESSION=<строка> SERVER_URL=https://ваш-сервер USERBOT_SECRET=<тот же, что на сервере> python3 userbot.py
"""
import asyncio, json, os, sys, urllib.request

API_ID = int(os.environ.get("TG_API_ID", "0") or 0)
API_HASH = os.environ.get("TG_API_HASH", "")
SESSION = os.environ.get("TG_SESSION", "")
SERVER_URL = os.environ.get("SERVER_URL", "").rstrip("/")
SECRET = os.environ.get("USERBOT_SECRET", "")


def post_deposit(payload):
    req = urllib.request.Request(
        SERVER_URL + "/api/userbot/nft", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Userbot-Secret": SECRET})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def gift_info(action):
    """Достаёт название, номер и модель NFT из service-сообщения (с запасом на разные версии Telethon)."""
    g = getattr(action, "gift", None)
    title = getattr(g, "title", None) or getattr(g, "slug", None) or "NFT"
    num = getattr(g, "num", 0) or 0
    model = ""
    for a in (getattr(g, "attributes", None) or []):
        if type(a).__name__ == "StarGiftAttributeModel":
            model = getattr(a, "name", "") or ""
    return title, num, model


async def main():
    from telethon import TelegramClient, events
    from telethon.sessions import StringSession

    if not (API_ID and API_HASH):
        raise SystemExit("Задайте TG_API_ID и TG_API_HASH (https://my.telegram.org)")

    if "--login" in sys.argv:
        async with TelegramClient(StringSession(), API_ID, API_HASH) as c:
            print("\nВаша строка сессии (держите в секрете):\n" + c.session.save() + "\n")
        return

    if not (SESSION and SERVER_URL and SECRET):
        raise SystemExit("Задайте TG_SESSION, SERVER_URL и USERBOT_SECRET")

    client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
    await client.start()
    me = await client.get_me()
    account = "@" + me.username if getattr(me, "username", None) else ""
    print("USERBOT OK:", account or me.id, flush=True)

    @client.on(events.NewMessage(incoming=True))
    async def on_msg(event):
        action = getattr(event.message, "action", None)
        if type(action).__name__ != "MessageActionStarGiftUnique":
            return  # не NFT-подарок
        if not event.is_private or not event.sender_id or event.sender_id == me.id:
            return
        title, num, model = gift_info(action)
        saved = getattr(action, "saved_id", None)
        payload = {
            "key": f"ub:{saved}" if saved else f"ub:{event.chat_id}:{event.message.id}",
            "user_id": event.sender_id, "name": f"{title}-{num}", "base": title, "num": num, "model": model,
            "account": account,
        }
        try:
            res = await asyncio.to_thread(post_deposit, payload)
            print("DEPOSIT", event.sender_id, title, num, res, flush=True)
        except Exception as e:
            print("DEPOSIT ERROR:", repr(e), flush=True)

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
