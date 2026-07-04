# -*- coding: utf-8 -*-
"""Ürün izleyicileri: kontrol döngüsü, canlı görev yöneticisi, heartbeat,
yedekleme ve sağlık özeti."""
import asyncio
import logging
import os
import random
import subprocess
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import BrowserContext

from . import konfig, veri
from .bildirim import Notifier
from .fiyat import kisa_tl, pct, tl
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

                # Ölü kaynak takibi: kartta görünür + haftada bir uyarı
                oluler = [s["url"] for s in sonuclar if s.get("dead")]
                if oluler:
                    st["olu_kaynaklar"] = oluler
                    if time.time() - st.get("dead_notify_ts", 0) > 7 * 24 * 3600:
                        st["dead_notify_ts"] = time.time()
                        await notifier.send(
                            f"🔗 {label} ürününün şu kayna(ğı/kları) ölmüş "
                            "görünüyor (sayfa kaldırılmış):\n"
                            + "\n".join(f"• {u}" for u in oluler)
                            + "\nKarttan ➕ ile yeni kaynak ekleyebilirsin.")
                elif st.pop("olu_kaynaklar", None):
                    pass  # kaynak dirildi — işareti kaldır

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
                        if best.get("pazar"):
                            st["pazar"] = best["pazar"]   # kartta gösterilir

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

                        # üyesi olduğu setlerin TOPLAM hedefini kontrol et
                        await set_toplam_kontrol(notifier, state, key)

                    if gerekli and not supheli:
                        thr2 = prod.get("price_threshold2_tl")
                        acil = bool(fp is not None and thr2 and fp <= float(thr2))
                        son_ts = st.get("last_notify_ts", 0)
                        son_fiyat = st.get("last_notify_price")
                        sustur = st.get("mute_until", 0)
                        cooldown_gecti = (datetime.now()
                                          - datetime.fromtimestamp(son_ts) > cooldown)
                        daha_da_dustu = (fp is not None and son_fiyat
                                         and fp <= son_fiyat * (1 - renotify_drop / 100))
                        # ACİL eşiği: cooldown beklemez ama kendi 3 saatlik
                        # frenine sahiptir (spam olmasın)
                        acil_zamani = acil and (time.time()
                                                - st.get("last_notify2_ts", 0) > 3 * 3600)
                        if time.time() < sustur:
                            logging.info(f"[{label}] hedefte ama susturulmuş "
                                         f"({datetime.fromtimestamp(sustur):%d.%m %H:%M}'e kadar).")
                        elif sessiz_saat_mi(settings) and not acil:
                            # state güncellenmez → sessizlik bitince kendiliğinden bildirir
                            logging.info(f"[{label}] hedefte ama sessiz saat — ertelendi.")
                        elif cooldown_gecti or daha_da_dustu or acil_zamani:
                            kaynak = best["host"]
                            if best.get("seller"):
                                kaynak += f" — satıcı: {best['seller']}"
                            ek = ""
                            baglam = veri.fiyat_baglami(label, fp) if fp else None
                            if baglam:
                                ek = (f"\n📊 {baglam['sinyal']} — 90g dip "
                                      f"{tl(baglam['dip90'])} · medyan {tl(baglam['medyan90'])}"
                                      f" · günlerin %{baglam['yuzde']}'inden ucuz")
                                if baglam["tum_dip"] is not None:
                                    ek += (f"\n🏆 Tüm zamanlar dibi: {tl(baglam['tum_dip'])}"
                                           f" ({baglam['tum_dip_tarih']})")
                            pazar = best.get("pazar")
                            if pazar:
                                ek += f"\n🏪 {pazar['satici_sayisi']} satıcı"
                                if pazar.get("ikinci_fiyat"):
                                    ek += f" · 2.si {tl(pazar['ikinci_fiyat'])}"
                                    if fp and pazar["ikinci_fiyat"] > fp * 1.2:
                                        ek += " ⚠️ tek satıcı belirgin ucuz — dikkat"
                                elif pazar["satici_sayisi"] == 1:
                                    ek += " ⚠️ (tek satıcı)"
                            onek = "🚨 ACİL — " if acil else "🔥 "
                            msg = f"{onek}{label}\n{detay}{ek}\n🌐 {kaynak}\n🔗 {best['url']}"
                            if await alarm_gonder(notifier, key, msg):
                                st["last_notify_ts"] = time.time()
                                st["last_notify_price"] = fp
                                if acil:
                                    st["last_notify2_ts"] = time.time()
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


def sessiz_saat_mi(settings: dict) -> bool:
    """quiet_hours ayarı ('0-8' gibi): bu aralıkta normal alarmlar ERTELENİR
    (state güncellenmediği için sessizlik bitince kendiliğinden tetiklenir);
    🚨 ACİL alarmlar her zaman geçer."""
    aralik = settings.get("quiet_hours")
    if not aralik:
        return False
    try:
        bas, son = (int(x) for x in str(aralik).split("-"))
    except ValueError:
        return False
    saat = datetime.now().hour
    if bas <= son:
        return bas <= saat < son
    return saat >= bas or saat < son      # gece yarısını aşan aralık (23-7)


def set_toplamlari(state: State) -> list[dict]:
    """Her set için canlı toplam: {'ad', 'hedef', 'toplam', 'eksik' (fiyatsız üye)}."""
    out = []
    for ad, s in konfig.setleri_getir().items():
        toplam, eksik = 0.0, []
        for key in s.get("urunler", []):
            fp = state.get(key).get("last_good_price")
            if fp:
                toplam += fp
            else:
                eksik.append(key)
        out.append({"ad": ad, "hedef": s.get("hedef"), "toplam": toplam,
                    "uyeler": list(s.get("urunler", [])), "eksik": eksik})
    return out


async def set_toplam_kontrol(notifier: Notifier, state: State, key: str) -> None:
    """Ürün fiyatı güncellenince, üyesi olduğu setlerin TOPLAM hedefini kontrol
    eder. Tüm üyelerin fiyatı okunmuşsa ve toplam hedefin altındaysa bildirir
    (set başına 24 saat cooldown). Parçalar tek tek hedefte olmasa bile toplam
    fırsatını yakalar — PC toplama senaryosunun kalbi."""
    for s in set_toplamlari(state):
        if key not in s["uyeler"] or s["eksik"] or not s["hedef"]:
            continue
        if s["toplam"] > float(s["hedef"]):
            continue
        st = state.get(f"_set:{s['ad']}")
        if time.time() - st.get("last_notify_ts", 0) < 24 * 3600:
            continue
        parcalar = "\n".join(
            f"  • {k}: {tl(state.get(k).get('last_good_price'))}"
            for k in s["uyeler"])
        if await notifier.send(
                f"📦🔥 SET HEDEFTE: {s['ad']}\n"
                f"💰 Toplam {tl(s['toplam'])} (hedef {tl(float(s['hedef']))})\n"
                f"{parcalar}"):
            st["last_notify_ts"] = time.time()
            await state.save()
            logging.warning(f"!!! SET BİLDİRİMİ: {s['ad']} !!!")


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


async def png_cek(context: BrowserContext, html: Path) -> Path:
    """Verilen grafik HTML'ini tarayıcıda açıp PNG'ye çeker (sendPhoto için)."""
    page = await context.new_page()
    try:
        await page.set_viewport_size({"width": 900, "height": 700})
        await page.goto(html.as_uri())
        await page.wait_for_timeout(1500)
        await page.screenshot(path=str(konfig.GRAFIK_PNG), full_page=True)
    finally:
        await page.close()
    return konfig.GRAFIK_PNG


async def grafik_png(context: BrowserContext, urun: str | None = None) -> Path:
    """fiyat_grafigi.html'i üretir + PNG çeker. urun verilirse tek ürün."""
    import grafik
    return await png_cek(context, grafik.generate(urun=urun))


def degisim_raporu(products: list, state: State) -> str:
    """Günlük özet: tam liste yerine sadece HAREKET — düşen/yükselen top 5,
    sorunlu sayısı, set toplamları. Hareket yoksa tek satır."""
    from datetime import date
    dun = (date.today() - timedelta(days=1)).isoformat()
    hareket, sorunlu = [], 0
    for p in products:
        if p.get("paused"):
            continue
        st = state.get(konfig.product_key(p))
        ts = st.get("last_good_ts")
        if not ts or time.time() - ts > 12 * 3600:
            sorunlu += 1
            continue
        simdi = st.get("last_good_price")
        onceki = (st.get("daily_min") or {}).get(dun)
        if simdi and onceki and onceki > 0:
            d = (simdi - onceki) / onceki * 100
            if abs(d) >= 0.5:
                hareket.append((d, p.get("label", "?"), onceki, simdi))

    satirlar = [f"✅ {len(products)} ürün izleniyor"
                + (f" · ⚠️ {sorunlu} okunamıyor (/sorunlu)" if sorunlu else "")]
    dusen = sorted(h for h in hareket if h[0] < 0)[:5]
    cikan = sorted((h for h in hareket if h[0] > 0), reverse=True)[:5]
    if dusen:
        satirlar.append("\n📉 Düşenler (24s):")
        satirlar += [f"• {ad}: {kisa_tl(o)}→{kisa_tl(s)} ({pct(d)})"
                     for d, ad, o, s in dusen]
    if cikan:
        satirlar.append("\n📈 Yükselenler (24s):")
        satirlar += [f"• {ad}: {kisa_tl(o)}→{kisa_tl(s)} ({pct(d)})"
                     for d, ad, o, s in cikan]
    if not dusen and not cikan:
        satirlar.append("Son 24 saatte kayda değer fiyat hareketi yok.")
    for s in set_toplamlari(state):
        eksik = f" ({len(s['eksik'])} üye fiyatsız)" if s["eksik"] else ""
        hedef = ""
        if s["hedef"] and not s["eksik"]:
            fark = s["toplam"] - float(s["hedef"])
            hedef = (" · 🔥 HEDEFTE" if fark <= 0
                     else f" · hedefe {kisa_tl(fark)}")
        satirlar.append(f"\n📦 {s['ad']}: {kisa_tl(s['toplam'])}{eksik}{hedef}")
    return "\n".join(satirlar)


# ===================== YEDEKLEME + SAĞLIK =====================

def _git_commit() -> str:
    try:
        r = subprocess.run(["git", "-C", str(konfig.BASE_DIR),
                            "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _sure(sn: float) -> str:
    if sn < 3600:
        return f"{sn/60:.0f} dk"
    if sn < 86400:
        return f"{sn/3600:.0f} sa {(sn % 3600)/60:.0f} dk"
    return f"{sn/86400:.0f} gün"


def _profil_mb() -> float:
    toplam = 0
    for kok, _, dosyalar in os.walk(konfig.USER_DATA_DIR):
        for d in dosyalar:
            try:
                toplam += os.path.getsize(os.path.join(kok, d))
            except OSError:
                pass
    return toplam / 1e6


async def yedek_al(notifier: Notifier, state: State) -> bool:
    """veri.db + state.json + telegram_urunler.yaml'ı zip'leyip Telegram'a
    gönderir — yedek senin sohbetinde, yani MAKİNE DIŞINDA durur (disk ölse de
    kurtarılır). products.yaml git'te, token BotFather'da olduğu için onlar
    yedeğe girmez. Son yedek zamanı state'e işlenir."""
    dosyalar = [d for d in (konfig.VERI_DB, konfig.STATE_FILE,
                            konfig.TELEGRAM_URUNLER) if d.exists()]
    if not dosyalar:
        return False
    zip_yol = konfig.BASE_DIR / "yedek.zip"
    try:
        with zipfile.ZipFile(zip_yol, "w", zipfile.ZIP_DEFLATED) as z:
            for d in dosyalar:
                z.write(d, d.name)
    except OSError as e:
        logging.warning(f"Yedek zip'lenemedi: {e}")
        return False
    tarih = datetime.now().strftime("%Y-%m-%d %H:%M")
    ok = await notifier.send_document(
        zip_yol, f"🗄 Yedek {tarih} — veri.db + state + ürün listesi.\n"
                 "Geri yükleme: bu dosyayı ~/tracker içine açman yeterli.")
    if ok:
        state.get("_meta")["last_backup_ts"] = time.time()
        await state.save()
        logging.info("Yedek Telegram'a gönderildi.")
    return ok


async def yedek_dongusu(notifier: Notifier, state: State, settings: dict) -> None:
    """Periyodik yedek (varsayılan 7 günde bir). 6 saatte bir kontrol eder;
    böylece makine kapalıyken kaçan yedek, açılınca kısa sürede tamamlanır."""
    gun = int(settings.get("backup_days", 7))
    if gun <= 0:
        return
    while True:
        son = state.get("_meta").get("last_backup_ts", 0)
        if time.time() - son > gun * 86400:
            try:
                await yedek_al(notifier, state)
            except Exception as e:
                logging.warning(f"Otomatik yedek alınamadı: {e}")
        await asyncio.sleep(6 * 3600)


def saglik_ozeti(products: list, state: State) -> str:
    """Telefondan öz-teşhis: ayakta süresi, okunamayanlar, disk, son yedek, sürüm."""
    meta = state.get("_meta")
    up = time.time() - meta.get("last_start_ts", time.time())
    sorunlu = [p for p in products if not p.get("paused")
               and (lambda st: not st.get("last_good_ts")
                    or time.time() - st.get("last_good_ts", 0) > 12 * 3600)
               (state.get(konfig.product_key(p)))]
    db = konfig.VERI_DB.stat().st_size / 1e6 if konfig.VERI_DB.exists() else 0
    son_yedek = meta.get("last_backup_ts")
    yedek_s = (_sure(time.time() - son_yedek) + " önce") if son_yedek else "henüz yok"
    satir = ["🩺 Sağlık",
             f"⏱ Ayakta: {_sure(up)}",
             f"📦 {len(products)} ürün"
             + (f" · ⚠️ {len(sorunlu)} okunamıyor (12s+)" if sorunlu
                else " · hepsi okunuyor ✅"),
             f"💾 veri.db {db:.1f} MB · profil {_profil_mb():.0f} MB",
             f"🗄 Son yedek: {yedek_s}"]
    commit = _git_commit()
    if commit:
        satir.append(f"🔖 Sürüm: {commit}")
    return "\n".join(satir)


async def heartbeat(notifier: Notifier, settings: dict, shared: dict,
                    state: State, context: BrowserContext) -> None:
    """Her gün belirli saatte değişenler raporu (bot yaşıyor sinyali).
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
        await notifier.send(degisim_raporu(products, state))
        gun = settings.get("weekly_chart_day", 0)
        if gun is not None and datetime.now().weekday() == int(gun):
            try:
                png = await grafik_png(context)
                await notifier.send_photo(png, "📈 Haftalık fiyat grafiği")
            except Exception as e:
                logging.warning(f"Haftalık grafik gönderilemedi: {e}")
