# -*- coding: utf-8 -*-
"""
STOK + FİYAT TAKİP BOTU — PRO
==============================
Motor    : Playwright (Chromium, kalıcı profil, stealth) → JS'li Türk sitelerinde çalışır
Bildirim : 1) Telegram Bot API   2) başarısızsa CallMeBot API (WhatsApp yedeği)
Yönetim  : Telegram'dan kart/butonlarla — /durum yaz, ürüne dokun, butonla yönet
Durum    : state.json → mükerrer bildirim yok (cooldown + tekrar-düşüş + susturma)
Geçmiş   : fiyat_gecmisi.csv → grafik.py ile HTML grafik + PNG rapor

Kod, takipbotu/ paketindedir (fiyat, konfig, veri, tarayici, karar, bildirim,
izleyici, arayuz) — bu dosya ince giriş noktasıdır; gözetmen (guncelleyici.py)
ve smoke testi burayı çağırır.

Kurulum:
  pip install -r requirements.txt && playwright install chromium
  python takip_botu_pro.py kur      → sihirbaz: token yapıştır + botuna /start yaz

Çalıştırma:
  python takip_botu_pro.py           → normal çalışma
  python3 guncelleyici.py            → 7/24 ÖNERİLEN: watchdog + git'ten otomatik
                                       güncelleme + bozuk push geri alma (README)
  python takip_botu_pro.py once      → tüm ürünleri BİR KEZ kontrol et, bildirim atma
  python takip_botu_pro.py test      → Telegram'a test mesajı gönder
  python takip_botu_pro.py chatid    → chat_id'ni öğren
  python takip_botu_pro.py grafik    → fiyat_grafigi.html üret
  python takip_botu_pro.py smoke     → sağlık kontrolü (gözetmen kullanır; çıkış 0=OK)
"""
import asyncio
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler

from playwright.async_api import async_playwright

from takipbotu import arayuz, izleyici, konfig, tarayici, veri
from takipbotu.bildirim import Notifier
from takipbotu.fiyat import tl

# Log disk disiplini: dosya 5 MB'ı geçince döner, en fazla 2 yedek tutulur
# (takip.log + .1 + .2 ≈ 15 MB tavan — sınırsız büyüme yok). Konsol çıktısı
# sadece elle çalıştırırken açılır; gözetmen/launchd altında kapalıdır ki
# launchd.log şişmesin (her şey zaten takip.log'da).
_log_handlers: list = [RotatingFileHandler(konfig.LOG_FILE, maxBytes=5_000_000,
                                           backupCount=2, encoding="utf-8")]
if sys.stderr.isatty():
    _log_handlers.append(logging.StreamHandler())
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=_log_handlers,
)
if os.getenv("STOCKBOT_DEBUG", "false").lower() == "true":
    logging.getLogger().setLevel(logging.DEBUG)


def _sites() -> tarayici.Sites:
    return tarayici.Sites(konfig.load_yaml(konfig.SITES_YAML))


async def main() -> None:
    settings, products = konfig.load_config()
    if not products:
        logging.error("products.yaml içinde aktif ürün yok.")
        return
    # Tek-kopya kilidi: aynı anda ikinci bot getUpdates çekip çakışmasın.
    # (Restart sırasında eski instance kapanana kadar bu kopya bekler; kilit
    # alınamazsa temiz çıkar, gözetmen tekrar dener.) Handle process boyunca
    # açık kalmalı → 'kilit' değişkeni main() kapsamında tutulur.
    kilit = konfig.tek_kopya_kilidi()
    if kilit is None:
        logging.error("Başka bir bot instance'ı çalışıyor (kilit alınamadı) — "
                      "bu kopya çıkıyor. Gözetmen birazdan tekrar dener.")
        return
    sites = _sites()
    veri.baslat()   # veri.db şeması + gerekiyorsa eski CSV'nin tek seferlik göçü
    state = veri.State(konfig.STATE_FILE)
    # Etiket değiştirilmişse geçmişi yeni ada taşı (URL parmak iziyle eşleşir)
    if veri.etiket_gocu(state, products):
        products = konfig.load_products()
        await state.save()
    throttle = tarayici.HostThrottle(float(settings.get("min_gap_per_host_seconds", 25)))

    async with async_playwright() as p:
        rq = await p.request.new_context()
        # getUpdates uzun-yoklaması için AYRI bağlantı: buton/edit istekleriyle
        # aynı havuzu paylaşıp birbirini aç bırakmasın (arayüz donmasın)
        rq_poll = await p.request.new_context()
        notifier = Notifier(rq, settings)
        await notifier.check_bot()
        # Telegram sayesinde QR/oturum derdi yok → varsayılan headless
        context = await tarayici.launch_context(p, bool(settings.get("headless", True)))

        # Açılış mesajı — watchdog yeniden başlatınca haber ver; ama bot kısa
        # aralıklarla arka arkaya başlıyorsa (çökme döngüsü) mesaj spam'i yapma
        meta = state.get("_meta")
        son_baslangic = meta.get("last_start_ts", 0)
        meta["last_start_ts"] = time.time()
        await state.save()
        if time.time() - son_baslangic > 1800:
            await notifier.send(f"🔄 Takip botu başlatıldı — {len(products)} ürün izleniyor. "
                                "(/durum ile yönet)")
        else:
            logging.warning("30 dk içinde ikinci başlatma — açılış mesajı atlandı "
                            "(çökme döngüsü koruması). Sık oluyorsa takip.log'a bak!")

        shared = {"products": products}
        manager = izleyici.WatcherManager(context, sites, throttle, notifier,
                                          state, settings)
        manager.sync(products)
        hb = asyncio.create_task(izleyici.heartbeat(notifier, settings, shared,
                                                    state, context))
        yd = asyncio.create_task(izleyici.yedek_dongusu(notifier, state, settings))
        lst = asyncio.create_task(arayuz.telegram_listener(
            notifier, shared, state, manager, context, poll_rq=rq_poll))
        logging.info(f"{len(products)} ürün izleniyor. Durdurmak için Ctrl+C. "
                     "Telegram'dan /durum yazabilirsin.")
        try:
            await asyncio.gather(hb, yd, lst)
        except (KeyboardInterrupt, asyncio.CancelledError):
            logging.info("Durduruluyor...")
        finally:
            izleyiciler = manager.cancel_all()
            yd.cancel()
            hb.cancel()
            lst.cancel()
            await asyncio.gather(*izleyiciler, hb, yd, lst, return_exceptions=True)
            await context.close()
            await rq.dispose()
            await rq_poll.dispose()


async def run_once() -> None:
    """Tüm ürünleri (tüm kaynaklarıyla) bir kez kontrol eder, tabloyu yazdırır,
    BİLDİRİM ATMAZ. Yeni ürün/site eklerken seçicileri denemek için kullan."""
    settings, products = konfig.load_config()
    sites = _sites()
    throttle = tarayici.HostThrottle(float(settings.get("min_gap_per_host_seconds", 25)))
    async with async_playwright() as p:
        context = await tarayici.launch_context(p, bool(settings.get("headless", True)))
        print(f"\n{'ÜRÜN':<38} {'SİTE':<18} {'FİYAT':>14} {'KAYNAK':>8} {'STOK':>5} VARYANT")
        print("-" * 100)
        for prod in products:
            for url in konfig.product_urls(prod):
                try:
                    s = await tarayici.check_once(context, sites, throttle, prod, url)
                    stok = {True: "VAR", False: "YOK", None: "?"}[s["in_stock"]]
                    engel = "  ⛔ BOT KORUMASI!" if s["blocked"] else ""
                    satici = f"  ({s['seller']})" if s.get("seller") else ""
                    print(f"{prod.get('label', '?')[:37]:<38} {s['host'][:17]:<18} "
                          f"{tl(s['price']):>14} {s['source']:>8} {stok:>5} "
                          f"{s['variant']}{satici}{engel}")
                except Exception as e:
                    print(f"{prod.get('label', '?')[:37]:<38} "
                          f"HATA: {type(e).__name__}: {e}")
        await context.close()


async def run_test() -> None:
    settings, _ = konfig.load_config()
    async with async_playwright() as p:
        rq = await p.request.new_context()
        n = Notifier(rq, settings)
        if not await n.check_bot():
            print("Token hatalı veya boş — products.yaml → telegram_bot_token")
            await rq.dispose()
            return
        ok = await n.send("✅ Takip botu kurulumu tamam! /durum ile ürünleri yönet.")
        print("Sonuç:", "GÖNDERİLDİ ✔" if ok else
              "GÖNDERİLEMEDİ ✖ — chat_id ayarlı mı? (python takip_botu_pro.py chatid)")
        await rq.dispose()


async def run_chatid() -> None:
    """Bota Telegram'dan /start (veya herhangi bir mesaj) yaz, sonra bunu çalıştır:
    gelen sohbetlerin chat_id'lerini listeler."""
    settings, _ = konfig.load_config()
    async with async_playwright() as p:
        rq = await p.request.new_context()
        n = Notifier(rq, settings)
        if not await n.check_bot():
            await rq.dispose()
            return
        print("Son mesajlar taranıyor (bota bir mesaj yazmış olmalısın)...")
        updates = await n.tg("getUpdates", timeout_ms=35000, timeout=20) or []
        gorulen = {}
        for u in updates:
            msg = u.get("message") or {}
            chat = msg.get("chat") or {}
            if chat.get("id"):
                gorulen[chat["id"]] = chat.get("first_name") or chat.get("title") or "?"
        if gorulen:
            for cid, ad in gorulen.items():
                print(f"  chat_id: {cid}   ({ad})")
            print("Bu değeri products.yaml → telegram_chat_id alanına yaz.")
        else:
            print("Mesaj bulunamadı. Telegram'dan botuna /start yazıp tekrar dene.")
        await rq.dispose()


async def run_kur() -> bool:
    """Kurulum sihirbazı: YAML düzenlemeden soru-cevapla Telegram'ı bağlar.
    True dönerse kullanıcı botun hemen başlatılmasını istedi demektir."""
    print()
    print("=" * 58)
    print("  TAKİP BOTU KURULUM SİHİRBAZI")
    print("=" * 58)
    print()
    print("ADIM 1/2 — Telegram botu oluştur:")
    print("  • Telegram'da @BotFather'ı aç")
    print("  • /newbot yaz, bir isim ver")
    print("  • Sana verdiği token'ı (123456:ABC-DEF... gibi) buraya yapıştır")
    print()
    async with async_playwright() as p:
        rq = await p.request.new_context()
        try:
            token = ""
            while True:
                try:
                    token = input("Bot token: ").strip()
                except EOFError:
                    return False
                if not token:
                    continue
                n = Notifier(rq, {"telegram_bot_token": token})
                me = await n.tg("getMe")
                if me:
                    print(f"  ✔ Bot bulundu: @{me.get('username')}")
                    break
                print("  ✖ Token geçersiz görünüyor, tekrar dene.")

            print()
            print("ADIM 2/2 — Botunla eşleş:")
            print(f"  • Telegram'da @{me.get('username')} sohbetini aç ve /start yaz")
            print("  • Mesajını bekliyorum (2 dakika)...")
            chat_id = None
            ad = ""
            offset = 0
            son = time.time() + 120
            while time.time() < son and not chat_id:
                updates = await n.tg("getUpdates", timeout_ms=25000,
                                     offset=offset, timeout=15) or []
                for u in updates:
                    offset = u["update_id"] + 1
                    chat = ((u.get("message") or {}).get("chat") or {})
                    if chat.get("id"):
                        chat_id = str(chat["id"])
                        ad = chat.get("first_name") or chat.get("title") or ""
            if not chat_id:
                print("  ✖ Mesaj gelmedi. Tekrar denemek için: "
                      "python takip_botu_pro.py kur")
                return False
            print(f"  ✔ Eşleşti: {ad} (chat_id: {chat_id})")

            konfig.save_yaml_atomic(konfig.KURULUM_YAML, {
                "telegram_bot_token": token,
                "telegram_chat_id": chat_id,
            })
            n.chat_id = chat_id
            await n.send("🎉 Kurulum tamam! Artık ürün eklemek için bana ürün "
                         "linkini göndermen yeterli. /durum ile yönet.")
            print()
            print("  ✔ Ayarlar kurulum.yaml'a kaydedildi, test mesajı gönderildi.")
            print()
            try:
                cevap = input("Bot şimdi başlatılsın mı? [E/h]: ").strip().lower()
            except EOFError:
                return False
            return cevap in ("", "e", "evet", "y", "yes")
        finally:
            await rq.dispose()


def _telegram_ayarli() -> bool:
    try:
        settings, _ = konfig.load_config()
        return bool(settings.get("telegram_bot_token"))
    except Exception:
        return True  # config okunamıyorsa main() kendi hatasını versin


if __name__ == "__main__":
    komut = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    try:
        if komut == "test":
            asyncio.run(run_test())
        elif komut == "once":
            asyncio.run(run_once())
        elif komut == "chatid":
            asyncio.run(run_chatid())
        elif komut == "kur":
            if asyncio.run(run_kur()):
                asyncio.run(main())
        elif komut == "grafik":
            import grafik
            print(f"Grafik üretildi: {grafik.generate()}")
        elif komut == "smoke":
            # Gözetmen (guncelleyici.py) güncelleme sonrası çağırır: kod import
            # edilebiliyor ve konfig yükleniyor mu? Çıkış 0 = sağlıklı; değilse
            # gözetmen push'u geri alır. Ağa/tarayıcıya dokunmaz, hızlıdır.
            try:
                settings, products = konfig.load_config()
                sites = _sites()
                import grafik  # noqa: F401 — grafik modülü de sağlam olsun
                print(f"SMOKE OK — {len(products)} ürün, "
                      f"{len(sites.sites)} site kuralı")
            except Exception as e:
                print(f"SMOKE FAIL: {type(e).__name__}: {e}")
                sys.exit(1)
        else:
            # İlk çalıştırma kolaylığı: Telegram hiç ayarlanmamışsa ve terminal
            # etkileşimliyse sihirbazı otomatik başlat
            if not _telegram_ayarli() and sys.stdin.isatty():
                print("Telegram ayarlı görünmüyor — kurulum sihirbazı başlıyor.")
                if asyncio.run(run_kur()):
                    asyncio.run(main())
            else:
                asyncio.run(main())
    except KeyboardInterrupt:
        pass
