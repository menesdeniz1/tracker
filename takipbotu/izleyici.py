# -*- coding: utf-8 -*-
"""Ürün izleyicileri: kontrol döngüsü, canlı görev yöneticisi, heartbeat."""
import asyncio
import logging
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import BrowserContext

from . import konfig, veri
from .bildirim import Notifier
from .fiyat import pct, tl
from .karar import alarm_gerekli, en_iyi_kaynak, fiyat_suphali
from .tarayici import HostThrottle, Sites, check_once
from .veri import State


async def check_product(context: BrowserContext, sites: Sites, throttle: HostThrottle,
                        prod: dict) -> list[dict]:
    """Ürünün TÜM kaynaklarını kontrol eder, okuma başına geçmişe yazar.
    Bir kaynağın hatası diğerlerini iptal etmez (Akakçe birincil kurgusunda
    tek kaynağın arızası ürünü kör etmesin); AMA tüm kaynaklar hata verirse
    hata yukarı fırlatılır ki izleyicinin error_streak sayacı çalışsın."""
    label = prod.get("label", "Ürün")
    sonuclar = []
    son_hata: Exception | None = None
    for url in konfig.product_urls(prod):
        try:
            s = await check_once(context, sites, throttle, prod, url)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            son_hata = e
            logging.warning(f"[{label}] kaynak okunamadı "
                            f"({urlparse(url).netloc}): {type(e).__name__}: {e}")
            continue
        sonuclar.append(s)
        if not s["blocked"] and s["price"] is not None:
            await veri.append_history(label, s["host"], s["price"],
                                      s["in_stock"], s["source"])
    if not sonuclar and son_hata is not None:
        raise son_hata
    return sonuclar


async def product_watcher(context: BrowserContext, sites: Sites, throttle: HostThrottle,
                          notifier: Notifier, prod: dict, state: State,
                          sem: asyncio.Semaphore, settings: dict) -> None:
    label = prod.get("label", "Ürün")
    key = konfig.product_key(prod)
    cooldown = timedelta(minutes=int(prod.get("cooldown_minutes", 1440)))
    renotify_drop = float(prod.get("renotify_drop_pct",
                                   settings.get("renotify_drop_pct", 3)))
    sanity_guard = bool(settings.get("sanity_guard", True))
    error_alert_streak = int(settings.get("error_alert_streak", 5))
    low30_alert = bool(settings.get("low30_alert", True))
    low30_min_days = int(settings.get("low30_min_days", 7))
    low30_cooldown = timedelta(minutes=int(settings.get("low30_cooldown_minutes", 1440)))

    while True:
        quick_recheck = False
        async with sem:
            st = state.get(key)
            st["urls"] = konfig.product_urls(prod)   # etiket göçü için parmak izi
            try:
                sonuclar = await check_product(context, sites, throttle, prod)
                best = en_iyi_kaynak(prod, sonuclar)
                hepsi_engelli = all(s["blocked"] for s in sonuclar)

                if best is not None and not hepsi_engelli:
                    fp = best["price"]
                    logging.info(f"[{label}] en iyi: {tl(fp)} ({best['host']}, "
                                 f"{best['source']}) stok={best['in_stock']} "
                                 f"varyant={best['variant']}")

                    # Fiyat modunda hiçbir kaynak fiyat okuyamadıysa bu da bir
                    # arıza belirtisidir → hata serisine say
                    mode = prod.get("mode", "price")
                    if "price" in mode and fp is None:
                        st["error_streak"] = int(st.get("error_streak", 0)) + 1
                        if st["error_streak"] == error_alert_streak:
                            await notifier.send(
                                f"⚠️ {label} üst üste {error_alert_streak} kontroldür "
                                "fiyat okunamıyor.\nSite değişmiş olabilir — "
                                "sites.yaml seçicisini kontrol et.")
                    else:
                        st["error_streak"] = 0

                    gerekli, detay = alarm_gerekli(prod, best)
                    # supheli bilerek 'gerekli'den bağımsız: şüpheli fiyat hedef
                    # alarmı tetiklemese de low30 sinyalini ve state'i kirletmesin
                    supheli = sanity_guard and fiyat_suphali(prod, best, st)

                    if supheli:
                        pend = st.get("pending_price")
                        # ikinci okuma öncekiyle ±%2 tutarlıysa fiyat gerçek kabul edilir
                        if pend and fp and abs(pend - fp) <= fp * 0.02:
                            st.pop("pending_price", None)
                            supheli = False
                            logging.info(f"[{label}] şüpheli fiyat ikinci okumayla "
                                         f"doğrulandı: {tl(fp)}")
                        else:
                            st["pending_price"] = fp
                            quick_recheck = True
                            logging.warning(f"[{label}] fiyat şüpheli görünüyor "
                                            f"({tl(fp)}, kaynak={best['source']}) — "
                                            "1-2 dk içinde ikinci okumayla doğrulanacak.")
                    else:
                        st.pop("pending_price", None)

                    if fp is not None and not supheli:
                        # 30-gün dibi bugünkü okuma işlenmeden ÖNCE hesaplanmalı
                        dip, gun_sayisi = veri.dip30_oncesi(st)
                        veri.gunluk_min_guncelle(st, fp)
                        st["last_good_price"] = fp
                        st["last_good_ts"] = time.time()
                        st["last_good_host"] = best["host"]

                        # 📉 Hedefe inmese bile "son 30 günün en düşüğü" bilgisi.
                        # Hedef alarmı zaten atılacaksa mükerrer mesaj atılmaz.
                        if (low30_alert and not gerekli and dip is not None
                                and gun_sayisi >= low30_min_days and fp < dip):
                            son_dip_ts = st.get("low30_notify_ts", 0)
                            if (datetime.now() - datetime.fromtimestamp(son_dip_ts)
                                    > low30_cooldown):
                                thr = prod.get("price_threshold_tl")
                                hedef_txt = f", hedef {tl(float(thr))}" if thr else ""
                                await notifier.send(
                                    f"📉 {label} son 30 günün en düşüğünde!\n"
                                    f"💰 {tl(fp)} (önceki dip {tl(dip)}{hedef_txt})\n"
                                    f"🌐 {best['host']}\n🔗 {best['url']}")
                                st["low30_notify_ts"] = time.time()
                                logging.info(f"[{label}] 30-gün dibi bildirildi: {tl(fp)}")

                    if gerekli and not supheli:
                        son_ts = st.get("last_notify_ts", 0)
                        son_fiyat = st.get("last_notify_price")
                        sustur = st.get("mute_until", 0)
                        cooldown_gecti = (datetime.now()
                                          - datetime.fromtimestamp(son_ts) > cooldown)
                        daha_da_dustu = (fp is not None and son_fiyat
                                         and fp <= son_fiyat * (1 - renotify_drop / 100))
                        if time.time() < sustur:
                            logging.info(f"[{label}] hedefte ama susturulmuş "
                                         f"({datetime.fromtimestamp(sustur):%d.%m %H:%M}'e kadar).")
                        elif cooldown_gecti or daha_da_dustu:
                            kaynak = best["host"]
                            if best.get("seller"):
                                kaynak += f" — satıcı: {best['seller']}"
                            msg = f"🔥 {label}\n{detay}\n🌐 {kaynak}\n🔗 {best['url']}"
                            if await alarm_gonder(notifier, key, msg):
                                st["last_notify_ts"] = time.time()
                                st["last_notify_price"] = fp
                                logging.warning(f"!!! BİLDİRİM: {label} !!!")
                        else:
                            logging.info(f"[{label}] hedefte ama cooldown sürüyor "
                                         "(mükerrer bildirim engellendi).")
                    await state.save()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logging.error(f"[{label}] beklenmedik hata: {type(e).__name__}: {e}")
                st["error_streak"] = int(st.get("error_streak", 0)) + 1
                # Üst üste N hata = büyük ihtimalle site değişti / seçici bozuldu → tek uyarı
                if st["error_streak"] == error_alert_streak:
                    await notifier.send(
                        f"⚠️ {label} üst üste {error_alert_streak} kontroldür okunamıyor.\n"
                        "Site değişmiş olabilir — sites.yaml seçicisini kontrol et.")
                await state.save()

        if quick_recheck:
            await asyncio.sleep(random.uniform(60, 120))
        else:
            await asyncio.sleep(random.randint(int(prod.get("sleep_min", 300)),
                                               int(prod.get("sleep_max", 600))))


async def alarm_gonder(notifier: Notifier, key: str, msg: str) -> bool:
    """Hedef alarmı — altında hızlı aksiyon butonlarıyla. Buton gönderimi
    başarısız olursa düz mesaja düşer (bildirim asla kaybolmaz)."""
    from . import arayuz
    kid = arayuz.kisa_id(key)
    rows = [[{"text": "✅ Aldım — izlemeyi bırak", "callback_data": f"aldim|{kid}"},
             {"text": "🔕 1 hafta sustur", "callback_data": f"sustur|{kid}"}],
            [{"text": "🎯 Hedefi değiştir", "callback_data": f"hedefsec|{kid}"},
             {"text": "📄 Ürün kartı", "callback_data": f"kart|{kid}"}]]
    if await notifier.send_buttons(msg, rows):
        return True
    return await notifier.send(msg)


class WatcherManager:
    """İzleyici görevlerini ürün anahtarına göre yönetir. Telegram'dan ürün
    eklenince/silinince/hedef değişince izleyiciler CANLI güncellenir —
    bot yeniden başlatılmaz. Duraklatılan ürünlerin görevi durdurulur."""

    def __init__(self, context: BrowserContext, sites: Sites, throttle: HostThrottle,
                 notifier: Notifier, state: State, settings: dict):
        self.context = context
        self.sites = sites
        self.throttle = throttle
        self.notifier = notifier
        self.state = state
        self.settings = settings
        self.sem = asyncio.Semaphore(int(settings.get("max_concurrency", 3)))
        self.tasks: dict[str, asyncio.Task] = {}
        # Kullanıcı işlemleri (link önizleme, Akakçe arama) için AYRI hızlı
        # şerit: izleyicilerin kuyruğuna/geri çekilmesine takılıp arayüzü
        # dondurmasın. İnsan tetiklediği için nazik bir aralık yeterli.
        self.hizli = HostThrottle(3.0)

    def sync(self, products: list[dict],
             force: "set[str] | frozenset[str]" = frozenset()) -> None:
        """Ürün listesini görevlerle eşitler. force'taki anahtarlar yeniden
        başlatılır (hedef değişikliği yeni ayarla devam etsin diye)."""
        istenen = {konfig.product_key(p): p for p in products
                   if not p.get("paused")}
        for key in list(self.tasks):
            if key not in istenen or key in force:
                self.tasks.pop(key).cancel()
                logging.info(f"[{key}] izleyici durduruldu.")
        for key, p in istenen.items():
            if key not in self.tasks or self.tasks[key].done():
                self.tasks[key] = asyncio.create_task(product_watcher(
                    self.context, self.sites, self.throttle, self.notifier,
                    p, self.state, self.sem, self.settings))
                logging.info(f"[{key}] izleyici başlatıldı.")

    def cancel_all(self) -> list[asyncio.Task]:
        for t in self.tasks.values():
            t.cancel()
        return list(self.tasks.values())


# ===================== ÖZET / GRAFİK =====================

def durum_ozeti(products: list, state: State) -> str:
    """Ürün başına son bilinen fiyat + hedef + 7 günlük trend + bayatlık işareti
    (heartbeat'in düz metin özeti; butonlu görünüm arayüz katmanındadır)."""
    satirlar = []
    for p in products:
        st = state.get(konfig.product_key(p))
        fp = st.get("last_good_price")
        ts = st.get("last_good_ts")
        host = st.get("last_good_host", "")
        thr = p.get("price_threshold_tl")
        bayat = ""
        if p.get("paused"):
            bayat = " ⏸ duraklatıldı"
        elif ts and time.time() - ts > 12 * 3600:
            bayat = " ⚠️ eski okuma!"
        elif not ts:
            bayat = " ⚠️ hiç okunamadı!"
        hedef = f" / hedef {tl(float(thr))}" if thr else ""
        kaynak = f" ({host})" if host else ""
        d7 = veri.yedi_gun_degisim(st)
        trend = f" {pct(d7)}/7g" if d7 is not None else ""
        satirlar.append(f"• {p.get('label', '?')}: {tl(fp)}{hedef}{trend}{kaynak}{bayat}")
    return "\n".join(satirlar)


async def grafik_png(context: BrowserContext, urun: str | None = None) -> Path:
    """fiyat_grafigi.html'i üretir, tarayıcıda açıp PNG'ye çeker (sendPhoto için).
    urun verilirse sadece o ürünün grafiği çizilir (kart → 📈 butonu)."""
    import grafik
    html = grafik.generate(urun=urun)
    page = await context.new_page()
    try:
        await page.set_viewport_size({"width": 900, "height": 700})
        await page.goto(html.as_uri())
        await page.wait_for_timeout(1500)
        await page.screenshot(path=str(konfig.GRAFIK_PNG), full_page=True)
    finally:
        await page.close()
    return konfig.GRAFIK_PNG


async def heartbeat(notifier: Notifier, settings: dict, shared: dict,
                    state: State, context: BrowserContext) -> None:
    """Her gün belirli saatte 'bot yaşıyor' + fiyat özeti + 7g trend.
    Haftada bir (weekly_chart_day) grafik PNG olarak da gelir."""
    saat = settings.get("heartbeat_hour")
    if saat is None:
        return
    while True:
        simdi = datetime.now()
        hedef = simdi.replace(hour=int(saat), minute=0, second=0, microsecond=0)
        if hedef <= simdi:
            hedef += timedelta(days=1)
        await asyncio.sleep((hedef - simdi).total_seconds())

        products = shared["products"]
        await notifier.send(f"✅ Takip botu çalışıyor — {len(products)} ürün izleniyor.\n"
                            + durum_ozeti(products, state))
        gun = settings.get("weekly_chart_day", 0)
        if gun is not None and datetime.now().weekday() == int(gun):
            try:
                png = await grafik_png(context)
                await notifier.send_photo(png, "📈 Haftalık fiyat grafiği")
            except Exception as e:
                logging.warning(f"Haftalık grafik gönderilemedi: {e}")
