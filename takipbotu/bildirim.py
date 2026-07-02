# -*- coding: utf-8 -*-
"""Bildirim katmanı: Telegram Bot API birincil, CallMeBot (WhatsApp) yedek."""
import asyncio
import logging
from pathlib import Path
from urllib.parse import quote_plus


class Notifier:
    """Birincil kanal: Telegram Bot API — düz HTTPS, tarayıcı/oturum derdi yok.
    3 deneme + üstel bekleme; hepsi patlarsa CallMeBot (WhatsApp) yedeğine düşer.
    İkisi de patlarsa ERROR log — sessiz kayıp yok."""

    def __init__(self, rq, settings: dict):
        self.rq = rq  # playwright APIRequestContext
        self.token = str(settings.get("telegram_bot_token", "") or "")
        self.chat_id = str(settings.get("telegram_chat_id", "") or "")
        self.phone = str(settings.get("phone", "")).lstrip("+")
        self.callmebot_key = str(settings.get("callmebot_apikey", "") or "")
        self.lock = asyncio.Lock()

    @property
    def api(self) -> str:
        return f"https://api.telegram.org/bot{self.token}"

    async def tg(self, method: str, timeout_ms: int = 30000, **params):
        """Telegram API çağrısı. Başarıda 'result' döner, hatada None."""
        if not self.token:
            return None
        try:
            resp = await self.rq.post(f"{self.api}/{method}", data=params,
                                      timeout=timeout_ms)
            js = await resp.json()
            if not js.get("ok"):
                logging.warning(f"Telegram {method} hatası: {js.get('description')}")
                return None
            return js.get("result")
        except Exception as e:
            logging.warning(f"Telegram {method} isteği başarısız: {e}")
            return None

    async def check_bot(self) -> bool:
        me = await self.tg("getMe")
        if me:
            logging.info(f"Telegram botu hazır: @{me.get('username')}")
            if not self.chat_id:
                logging.warning("telegram_chat_id boş! Bota /start yaz, sonra: "
                                "python takip_botu_pro.py chatid")
            return True
        logging.error("Telegram botuna ulaşılamadı — products.yaml → "
                      "telegram_bot_token alanını kontrol et.")
        return False

    async def _send_telegram(self, text: str, chat_id: str | None = None) -> bool:
        cid = chat_id or self.chat_id
        if not self.token or not cid:
            return False
        for bekle in (0, 2, 4):
            if bekle:
                await asyncio.sleep(bekle)
            r = await self.tg("sendMessage", chat_id=cid, text=text,
                              disable_web_page_preview=True)
            if r:
                return True
        return False

    async def _send_callmebot(self, text: str) -> bool:
        if not self.callmebot_key or not self.phone:
            return False
        try:
            url = (f"https://api.callmebot.com/whatsapp.php?phone=%2B{self.phone}"
                   f"&text={quote_plus(text)}&apikey={quote_plus(self.callmebot_key)}")
            resp = await self.rq.get(url, timeout=30000)
            if not resp.ok:
                return False
            body = (await resp.text()).lower()
            return "invalid" not in body and "error" not in body
        except Exception as e:
            logging.warning(f"CallMeBot gönderimi başarısız: {e}")
            return False

    async def send(self, text: str) -> bool:
        async with self.lock:
            if await self._send_telegram(text):
                logging.info("Bildirim Telegram ile gönderildi ✔")
                return True
            if await self._send_callmebot(text):
                logging.info("Bildirim CallMeBot (yedek kanal) ile gönderildi ✔")
                return True
            logging.error("BİLDİRİM GÖNDERİLEMEDİ! (Telegram + CallMeBot ikisi de başarısız)")
            return False

    async def _send_file(self, method: str, field: str, path: Path,
                         mime: str, caption: str) -> bool:
        if not self.token or not self.chat_id:
            return False
        try:
            resp = await self.rq.post(f"{self.api}/{method}", multipart={
                "chat_id": self.chat_id,
                "caption": caption,
                field: {"name": path.name, "mimeType": mime,
                        "buffer": path.read_bytes()},
            }, timeout=120000)
            js = await resp.json()
            if not js.get("ok"):
                logging.warning(f"Telegram {method} hatası: {js.get('description')}")
            return bool(js.get("ok"))
        except Exception as e:
            logging.warning(f"Telegram dosya gönderimi başarısız: {e}")
            return False

    async def send_document(self, path: Path, caption: str = "") -> bool:
        mime = {".csv": "text/csv", ".html": "text/html"}.get(
            path.suffix.lower(), "application/octet-stream")
        return await self._send_file("sendDocument", "document", path, mime, caption)

    async def send_photo(self, path: Path, caption: str = "") -> bool:
        return await self._send_file("sendPhoto", "photo", path, "image/png", caption)

    async def send_buttons(self, text: str, buttons: list[list[dict]]) -> bool:
        """Tıklanabilir buton satırlarıyla mesaj."""
        r = await self.tg("sendMessage", chat_id=self.chat_id, text=text,
                          disable_web_page_preview=True,
                          reply_markup={"inline_keyboard": buttons})
        return bool(r)

    async def edit_buttons(self, message_id, text: str,
                           buttons: list[list[dict]]) -> bool:
        """Mevcut mesajı yerinde günceller (kart gezinmesi sohbeti kirletmesin).
        Düzenleme başarısız olursa (mesaj eski vb.) yeni mesaj gönderilir."""
        r = await self.tg("editMessageText", chat_id=self.chat_id,
                          message_id=message_id, text=text,
                          disable_web_page_preview=True,
                          reply_markup={"inline_keyboard": buttons})
        if r:
            return True
        return await self.send_buttons(text, buttons)
