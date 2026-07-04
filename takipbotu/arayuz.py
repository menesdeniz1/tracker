# -*- coding: utf-8 -*-
"""Telegram arayüzü — kart/buton temelli, sıfır numara ezberi.

Tasarım ilkeleri:
  • Tek giriş: /durum — özet başlık + tıklanabilir ürün satırları (sorunlular
    üstte). Satıra dokun → ürün kartı → tüm işlemler butonla.
  • Kart gezinmesi mesajı YERİNDE düzenler (editMessageText) — sohbet temiz kalır.
  • Ürünler butonlarda kalıcı kısa ID ile taşınır (etiketin sha1'inden türetilir,
    64 baytlık callback_data sınırına sığar, yeniden başlatmada değişmez).
  • Düz metin de anlaşılır: link → ekleme akışı, sayı → beklenen hedef,
    başka metin → isimle bulanık arama.
Eski numaralı komutlar (/sil 3 gibi) geriye uyum için çalışmaya devam eder."""
import asyncio
import hashlib
import logging
import re
import time

from playwright.async_api import BrowserContext

from . import konfig, veri
from .bildirim import Notifier
from .fiyat import kisa_tl, parse_try_amount, pct, tl
from .izleyici import (
    WatcherManager,
    grafik_png,
    png_cek,
    saglik_ozeti,
    set_toplamlari,
    yedek_al,
)
from .tarayici import akakce_ara, check_once
from .veri import State

SAYFA_BOYU = 8
BARLAR = "▁▂▃▄▅▆▇█"

YARDIM = ("🏠 /menu yaz — gerisini butonlarla yürüt. Her ekranın altında\n"
          "[⬅️ Geri] [🏠 Menü] var; komut ezberlemene gerek yok.\n\n"
          "🛒 Ürün eklemek: linkini DİREKT GÖNDER — fiyatı okur, hedefi\n"
          "butonla seçtiririm.\n"
          "🔎 Ürün adı yaz (örn: 'kingston') → kartı direkt açılır.\n\n"
          "Menüden: 📊 Durum · ⚠️ Sorunlular · 📦 Setler · 📈 Grafik.\n"
          "Karttan: hedef/acil hedef, Akakçe'ye bağla, sete ekle, duraklat,\n"
          "sil (geri al'lı) — hepsi butonla.")

KOMUTLAR = [
    {"command": "menu", "description": "🏠 Ana menü — her şeye buradan"},
    {"command": "durum", "description": "Tüm ürünler — tıklanabilir liste"},
    {"command": "setler", "description": "Ürün grupları: canlı toplam + set hedefi"},
    {"command": "sorunlu", "description": "Sadece okunamayan/engelli ürünler"},
    {"command": "grafik", "description": "Fiyat grafiği (PNG + HTML)"},
    {"command": "saglik", "description": "Bot sağlığı + yedek durumu"},
    {"command": "csv", "description": "Ham fiyat geçmişi dosyası"},
    {"command": "yardim", "description": "Komutlar ve ipuçları"},
]


# ===================== KISA ID =====================

def kisa_id(key: str) -> str:
    """Ürün anahtarını 10 karakterlik kalıcı kimliğe indirger (callback_data
    64 bayt sınırı; etiket Türkçe/uzun olabilir). Deterministik: yeniden
    başlatmada değişmez, eski mesajlardaki butonlar çalışmaya devam eder."""
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def _mute_temizle(state: State, key: str) -> None:
    """Hedef değişince susturma kalkar — kullanıcı yeni hedeften alarm bekler."""
    state.get(key).pop("mute_until", None)


def key_bul(products: list[dict], kid: str) -> str | None:
    for p in products:
        k = konfig.product_key(p)
        if kisa_id(k) == kid:
            return k
    return None


def urun_bul(products: list[dict], kid: str) -> dict | None:
    for p in products:
        if kisa_id(konfig.product_key(p)) == kid:
            return p
    return None


# ===================== GEZİNME (profesyonel bot iskeleti) =====================
# Her ekran mesajı YERİNDE güncellenir (editMessageText) ve altında standart
# gezinme satırı taşır: [⬅️ Geri] [🏠 Menü]. "Geri" bağlamsaldır — karta setten
# gelindiyse sete, listeden gelindiyse listeye döner (kaynak callback_data'da
# taşınır, shared'de saklanır). Böylece bir kez /menu yazılır, gerisi butonla.

def _nav(geri_cb: str | None) -> list[dict]:
    """Ekran altı gezinme satırı. geri_cb None ise sadece 🏠 Menü."""
    row = []
    if geri_cb:
        row.append({"text": "⬅️ Geri", "callback_data": geri_cb})
    row.append({"text": "🏠 Menü", "callback_data": "menu"})
    return row


def _kart_geri(shared: dict, kid: str) -> str:
    """Kartın 'geri' hedefi: karta nereden ve HANGİ SAYFADAN gelindiyse oraya
    döner. ori biçimleri: 'd<sayfa>' (liste), 'sor<sayfa>' (sorunlu),
    's<sid>' (set). sid sha1-hex olduğu için 'o'/'r' içermez → 'sor' ile
    çakışmaz. Sayfa numarası, 3. sayfadan girip geri basınca yine 3. sayfaya
    dönmeyi sağlar (eskiden hep 1. sayfaya atıyordu)."""
    ori = (shared.get("kart_ori") or {}).get(kid, "d")
    if ori.startswith("sor"):
        return f"sor|{ori[3:] or 0}"
    if ori.startswith("s") and len(ori) > 1:      # set (s + 10 haneli sid)
        return f"set|{ori[1:]}"
    if ori.startswith("d"):
        return f"d|{ori[1:] or 0}"
    return "d|0"


def ana_menu_gorunumu(products: list[dict], state: State) -> tuple[str, list]:
    """🏠 Ana menü — her şeyin buton olduğu tek giriş ekranı."""
    sorunlu = sum(1 for p in products
                  if sorun_metni(p, state.get(konfig.product_key(p))))
    setler = konfig.setleri_getir()
    rows = [[{"text": f"📊 Durum ({len(products)})", "callback_data": "d|0"}]]
    if sorunlu:
        rows.append([{"text": f"⚠️ Sorunlular ({sorunlu})", "callback_data": "sor|0"}])
    rows.append([{"text": f"📦 Setler ({len(setler)})", "callback_data": "setler"},
                 {"text": "📈 Grafik", "callback_data": "grftum"}])
    rows.append([{"text": "🩺 Sağlık", "callback_data": "saglik"},
                 {"text": "❓ Yardım", "callback_data": "yardim"}])
    metin = ("🏠 Ana Menü — ne yapmak istersin?\n"
             "İpucu: ürün linki gönder → eklerim · ürün adı yaz → kartı açılır")
    return metin, rows


# ===================== GÖRÜNÜM KURUCULAR (saf — testlenebilir) =====================

def sorun_metni(p: dict, st: dict) -> str | None:
    """Ürünün dikkat isteyen durumu; yoksa None."""
    if p.get("paused"):
        return None                       # duraklatma sorun değil, tercih
    ts = st.get("last_good_ts")
    if not ts:
        return "hiç okunamadı"
    saat = (time.time() - ts) / 3600
    if saat > 12:
        return f"{saat/24:.0f} gündür okunamıyor" if saat > 48 else \
               f"{saat:.0f} saattir okunamıyor"
    if int(st.get("error_streak", 0)) >= 3:
        return "okunamıyor"
    return None


def _kisa_yas(st: dict) -> str:
    """Sorunlu ürünün sağ sütunu için kısa yaş: 'hiç' / '14s' / '3g'."""
    ts = st.get("last_good_ts")
    if not ts:
        return "hiç"
    saat = (time.time() - ts) / 3600
    return f"{saat/24:.0f}g" if saat > 48 else f"{saat:.0f}s"


def urun_satiri(p: dict, st: dict, ori: str = "d") -> list[dict]:
    """Durum listesindeki İKİ sütunlu ürün satırı: sol=ad, sağ=fiyat/durum.
    Telefonda tek uzun buton kırpılıp fiyatı yutuyordu; iki sütunda fiyat
    HER ZAMAN görünür. İki buton da aynı kartı açar. ori = kartın 'nereden
    gelindiği' işareti (d=liste, sor=sorunlu, s<sid>=set) → bağlamsal geri."""
    kid = kisa_id(konfig.product_key(p))
    cb = f"kart|{kid}|{ori}"
    label = p.get("label", "?")
    if p.get("paused"):
        sag = "⏸ durdu"
    else:
        sorun = sorun_metni(p, st)
        fp = st.get("last_good_price")
        thr = p.get("price_threshold_tl")
        if sorun:
            sag = f"⚠️ {_kisa_yas(st)} okumadı"
        elif fp and thr and fp <= float(thr):
            sag = f"🔥 {kisa_tl(fp)}"
        elif fp and thr:
            # güncel fiyat → hedef fiyat (yüzde yerine somut rakam)
            sag = f"{kisa_tl(fp)}→{kisa_tl(float(thr))}"
        else:
            sag = kisa_tl(fp)
    return [{"text": label[:32], "callback_data": cb},
            {"text": sag, "callback_data": cb}]


def _sirala(products: list[dict], state: State) -> list[dict]:
    """Sorunlular üstte, sonra hedefe yakınlık, duraklatılanlar en altta."""
    def skor(p):
        st = state.get(konfig.product_key(p))
        if p.get("paused"):
            return (2, 0)
        if sorun_metni(p, st):
            return (0, 0)
        fp, thr = st.get("last_good_price"), p.get("price_threshold_tl")
        yakinlik = (fp - float(thr)) / float(thr) if fp and thr else 9
        return (1, yakinlik)
    return sorted(products, key=skor)


def durum_gorunumu(products: list[dict], state: State, sayfa: int = 0,
                   sadece_sorunlu: bool = False) -> tuple[str, list]:
    """Birleşik durum: özet başlık + ürün başına tıklanabilir satır + sayfalama."""
    sorunlu = [p for p in products
               if sorun_metni(p, state.get(konfig.product_key(p)))]
    duraklatilmis = [p for p in products if p.get("paused")]

    def _hedefte(p: dict) -> bool:
        st = state.get(konfig.product_key(p))
        fp, thr = st.get("last_good_price"), p.get("price_threshold_tl")
        return bool(fp and thr and fp <= float(thr))

    hedefte = [p for p in products if _hedefte(p)]
    ozet = (f"📊 {len(products)} ürün · ✅ {len(products)-len(sorunlu)-len(duraklatilmis)}"
            + (f" · 🔥 {len(hedefte)} hedefte" if hedefte else "")
            + (f" · ⚠️ {len(sorunlu)} sorunlu" if sorunlu else "")
            + (f" · ⏸ {len(duraklatilmis)}" if duraklatilmis else ""))

    havuz = _sirala(sorunlu if sadece_sorunlu else products, state)
    on_ek = "sor" if sadece_sorunlu else "d"
    if sadece_sorunlu and not havuz:
        return ("✅ Sorunlu ürün yok — her şey okunuyor.",
                [_nav("d|0")])

    toplam_sayfa = max(1, (len(havuz) + SAYFA_BOYU - 1) // SAYFA_BOYU)
    sayfa = max(0, min(sayfa, toplam_sayfa - 1))
    dilim = havuz[sayfa * SAYFA_BOYU:(sayfa + 1) * SAYFA_BOYU]

    rows = []
    for p in dilim:
        # kart origin'ine sayfa numarasını da göm → geri basınca aynı sayfaya dön
        rows.append(urun_satiri(p, state.get(konfig.product_key(p)),
                                f"{on_ek}{sayfa}"))
    if toplam_sayfa > 1:
        rows.append([{"text": "◀️", "callback_data": f"{on_ek}|{sayfa-1}"},
                     {"text": f"{sayfa+1}/{toplam_sayfa}", "callback_data": f"{on_ek}|{sayfa}"},
                     {"text": "▶️", "callback_data": f"{on_ek}|{sayfa+1}"}])
    alt = []
    if not sadece_sorunlu and sorunlu:
        alt.append({"text": f"⚠️ Sorunlular ({len(sorunlu)})", "callback_data": "sor|0"})
    if konfig.setleri_getir():
        alt.append({"text": "📦 Setler", "callback_data": "setler"})
    alt.append({"text": "📈 Grafik", "callback_data": "grftum"})
    rows.append(alt)
    # sorunlu görünümde geri = tüm liste; ana listede geri = menü
    rows.append(_nav("d|0") if sadece_sorunlu else [{"text": "🏠 Menü",
                                                     "callback_data": "menu"}])

    baslik = "⚠️ Sorunlu ürünler:" if sadece_sorunlu else ozet
    return baslik + "\n(ürüne dokun → kart açılır)", rows


def sparkline(st: dict, gun: int = 14) -> str:
    """Günlük minimumlardan son N günün mini grafiği (▁▂▄▆█)."""
    from datetime import date, timedelta
    dmin = st.get("daily_min") or {}
    bugun = date.today()
    degerler = []
    for i in range(gun - 1, -1, -1):
        g = (bugun - timedelta(days=i)).isoformat()
        if g in dmin:
            degerler.append(dmin[g])
    if len(degerler) < 2:
        return ""
    lo, hi = min(degerler), max(degerler)
    if hi == lo:
        return BARLAR[3] * len(degerler)
    return "".join(BARLAR[round((v - lo) / (hi - lo) * (len(BARLAR) - 1))]
                   for v in degerler)


def _sure_metni(ts: float | None) -> str:
    if not ts:
        return "hiç"
    dk = (time.time() - ts) / 60
    if dk < 60:
        return f"{dk:.0f} dk önce"
    if dk < 48 * 60:
        return f"{dk/60:.0f} saat önce"
    return f"{dk/1440:.0f} gün önce"


def kart_gorunumu(p: dict, st: dict, geri_cb: str = "d|0") -> tuple[str, list]:
    """Ürün kartı: tüm bilgi + tüm işlemler tek ekranda. geri_cb = bağlamsal
    geri hedefi (setten gelindiyse sete, listeden gelindiyse listeye)."""
    key = konfig.product_key(p)
    kid = kisa_id(key)
    label = p.get("label", "?")
    fp = st.get("last_good_price")
    thr = p.get("price_threshold_tl")
    host = st.get("last_good_host", "")

    satirlar = [f"🛒 {label}"]
    if p.get("paused"):
        satirlar.append("⏸ İzleme duraklatıldı")
    sorun = sorun_metni(p, st)
    if sorun:
        satirlar.append(f"⚠️ {sorun}")
    if st.get("olu_kaynaklar"):
        satirlar.append(f"🔗 {len(st['olu_kaynaklar'])} kaynak ÖLÜ (sayfa "
                        "kaldırılmış) — ➕ ile yenisini ekle")

    fiyat_s = f"💰 {tl(fp)}" + (f" ({host})" if host else "")
    if thr:
        fiyat_s += f" · 🎯 {tl(float(thr))}"
        if fp:
            kalan = fp - float(thr)
            fiyat_s += " · HEDEFTE 🔥" if kalan <= 0 else f" ({kisa_tl(kalan)} kaldı)"
    thr2 = p.get("price_threshold2_tl")
    if thr2:
        fiyat_s += f" · 🚨 {kisa_tl(float(thr2))}"
    satirlar.append(fiyat_s)

    # "Bu iyi bir fiyat mı?" bağlamı (veri.db'den; 5+ günlük veri gerekir)
    baglam = veri.fiyat_baglami(label, fp) if fp else None
    if baglam:
        satirlar.append(f"📊 {baglam['sinyal']} · 90g dip {kisa_tl(baglam['dip90'])}"
                        f" · medyan {kisa_tl(baglam['medyan90'])}"
                        f" · günlerin %{baglam['yuzde']}'inden ucuz")
        if baglam["tum_dip"] is not None:
            satirlar.append(f"🏆 Tüm zamanlar dibi: {kisa_tl(baglam['tum_dip'])}"
                            f" ({baglam['tum_dip_tarih']})")

    pazar = st.get("pazar")
    if pazar:
        pazar_s = f"🏪 {pazar['satici_sayisi']} satıcı"
        if pazar.get("ikinci_fiyat"):
            pazar_s += f" · 2. en ucuz: {kisa_tl(pazar['ikinci_fiyat'])}"
            if fp and pazar["ikinci_fiyat"] > fp * 1.2:
                pazar_s += " ⚠️ tek satıcı belirgin ucuz"
        elif pazar["satici_sayisi"] == 1:
            pazar_s += " ⚠️ tek satıcı"
        satirlar.append(pazar_s)

    uyesi = [ad for ad, s in konfig.setleri_getir().items()
             if key in s.get("urunler", [])]
    if uyesi:
        satirlar.append("📦 Set: " + ", ".join(uyesi))

    dip, gun_sayisi = veri.dip30_oncesi(st)
    d7 = veri.yedi_gun_degisim(st)
    bilgi = []
    if d7 is not None:
        bilgi.append(f"7g: {pct(d7)}")
    if dip is not None and gun_sayisi >= 3:
        bilgi.append(f"30g dip: {tl(dip)}")
    bilgi.append(f"son okuma: {_sure_metni(st.get('last_good_ts'))}")
    satirlar.append("📈 " + " · ".join(bilgi))

    cizgi = sparkline(st)
    if cizgi:
        satirlar.append(f"📉 {cizgi}")

    urls = konfig.product_urls(p)
    hostlar = ", ".join(dict.fromkeys(
        u.split("/")[2].replace("www.", "") for u in urls if "://" in u))
    satirlar.append(f"🔗 {len(urls)} kaynak: {hostlar[:80]}")

    duraklat = ({"text": "▶️ Devam ettir", "callback_data": f"devam|{kid}"}
                if p.get("paused") else
                {"text": "⏸ Duraklat", "callback_data": f"durdur|{kid}"})
    rows = [
        [{"text": "🎯 Hedef değiştir", "callback_data": f"hedefsec|{kid}"},
         {"text": "🔍 Akakçe'ye bağla", "callback_data": f"akk|{kid}"}],
        [duraklat,
         {"text": "➕ Kaynak ekle", "callback_data": f"kaynak|{kid}"}],
        [{"text": "📦 Sete ekle", "callback_data": f"setsec|{kid}"},
         {"text": "📈 Grafiği", "callback_data": f"grf|{kid}"}],
        [{"text": "🗑 Sil", "callback_data": f"sil|{kid}"},
         {"text": "🛒 Ürüne git", "url": urls[0]}],
        _nav(geri_cb),
    ]
    return "\n".join(satirlar), rows


def hedef_secim_gorunumu(p: dict, st: dict) -> tuple[str, list]:
    """Hedef değiştirme menüsü: mevcut fiyattan %'li butonlar + elle giriş."""
    key = konfig.product_key(p)
    kid = kisa_id(key)
    fp = st.get("last_good_price")
    thr = p.get("price_threshold_tl")
    thr2 = p.get("price_threshold2_tl")
    metin = (f"🎯 {p.get('label', '?')}\n"
             f"Mevcut hedef: {tl(float(thr)) if thr else '—'}"
             + (f" · 🚨 acil: {tl(float(thr2))}" if thr2 else "")
             + (f"\nGüncel fiyat: {tl(fp)}" if fp else ""))
    rows = []
    if fp:
        rows += [[{"text": f"%3 altı → {tl(round(fp*0.97))}", "callback_data": f"hpct|{kid}|3"}],
                 [{"text": f"%5 altı → {tl(round(fp*0.95))}", "callback_data": f"hpct|{kid}|5"}],
                 [{"text": f"%10 altı → {tl(round(fp*0.90))}", "callback_data": f"hpct|{kid}|10"}]]
    rows.append([{"text": "✍️ Elle yazacağım", "callback_data": f"helle|{kid}"},
                 {"text": "🚨 Acil hedef", "callback_data": f"hacil|{kid}"}])
    rows.append(_nav(f"kart|{kid}"))
    return metin, rows


# ===================== SET GÖRÜNÜMLERİ =====================

def _bar(oran: float, uzunluk: int = 10) -> str:
    """0..1 oranı blok çubuğa çevirir: [███████░░░] gibi. Set toplamı hedefe
    yaklaştıkça (hedef/toplam oranı 1'e gittikçe) çubuk dolar."""
    oran = max(0.0, min(1.0, oran))
    dolu = round(oran * uzunluk)
    return "[" + "█" * dolu + "░" * (uzunluk - dolu) + "]"


def set_bul(sid: str) -> str | None:
    for ad in konfig.setleri_getir():
        if kisa_id(ad) == sid:
            return ad
    return None


def setler_gorunumu(state: State) -> tuple[str, list]:
    """Set listesi: her set bir buton (ad + üye sayısı + canlı toplam)."""
    rows = []
    for s in set_toplamlari(state):
        eksik = "*" if s["eksik"] else ""
        rows.append([{"text": f"📦 {s['ad']} ({len(s['uyeler'])}) — "
                              f"{kisa_tl(s['toplam'])}{eksik}",
                      "callback_data": f"set|{kisa_id(s['ad'])}"}])
    if not rows:
        return ("Henüz set yok. Ürün kartındaki 📦 Sete ekle butonuyla kur "
                "(örn. 'PC Toplama').",
                [_nav("d|0")])
    rows.append(_nav("d|0"))
    return "📦 Setlerin: (* = fiyatı okunamayan üye var)", rows


def set_gorunumu(ad: str, products: list[dict], state: State) -> tuple[str, list]:
    """Set kartı: canlı toplam + dünle kıyas + hedef + üyeler (fiyatlarıyla)."""
    from datetime import date, timedelta
    bilgi = next((s for s in set_toplamlari(state) if s["ad"] == ad), None)
    if bilgi is None:
        return "Bu set artık yok.", [_nav("setler")]
    sid = kisa_id(ad)
    satirlar = [f"📦 {ad} ({len(bilgi['uyeler'])} parça)"]
    toplam_s = f"💰 TOPLAM: {tl(bilgi['toplam'])}"
    if bilgi["eksik"]:
        toplam_s += f" (+{len(bilgi['eksik'])} üye fiyatsız!)"
    # dünle kıyas: tüm üyelerin dünkü günlük minimumu varsa
    dun = (date.today() - timedelta(days=1)).isoformat()
    dun_fiyatlar = [f for f in ((state.get(k).get("daily_min") or {}).get(dun)
                                for k in bilgi["uyeler"]) if f is not None]
    if len(dun_fiyatlar) == len(bilgi["uyeler"]) and not bilgi["eksik"]:
        dun_toplam = sum(dun_fiyatlar)
        if dun_toplam > 0:
            d = (bilgi["toplam"] - dun_toplam) / dun_toplam * 100
            toplam_s += f" · dün {kisa_tl(dun_toplam)} ({pct(d)})"
    satirlar.append(toplam_s)
    if bilgi["hedef"]:
        fark = bilgi["toplam"] - float(bilgi["hedef"])
        satirlar.append(f"🎯 Set hedefi: {tl(float(bilgi['hedef']))}"
                        + (" · HEDEFTE 🔥" if fark <= 0 and not bilgi["eksik"]
                           else f" ({kisa_tl(fark)} kaldı)"))
    satirlar.append("(pahalıdan ucuza)")

    # Üyeleri fiyata göre pahalıdan ucuza sırala (fiyatsızlar en altta)
    uyeler = sorted(
        bilgi["uyeler"],
        key=lambda k: state.get(k).get("last_good_price") or -1, reverse=True)
    rows = []
    for key in uyeler:
        p = next((q for q in products if konfig.product_key(q) == key), None)
        if p is not None:
            rows.append(urun_satiri(p, state.get(key), f"s{sid}"))

    # Alt bar: toplam + hedefe ilerleme çubuğu (toplam hedefe indikçe dolar)
    if bilgi["hedef"] and not bilgi["eksik"] and bilgi["toplam"] > 0:
        hedef = float(bilgi["hedef"])
        oran = min(1.0, hedef / bilgi["toplam"])
        if bilgi["toplam"] <= hedef:
            kuyruk = "🔥 HEDEFTE"
        else:
            kuyruk = f"→ 🎯 {kisa_tl(hedef)}"
        satirlar.append(f"\n{_bar(oran)} {kisa_tl(bilgi['toplam'])} {kuyruk}")
    else:
        satirlar.append(f"\n💰 TOPLAM: {tl(bilgi['toplam'])}")

    rows.append([{"text": "🎯 Set hedefi", "callback_data": f"sethedef|{sid}"},
                 {"text": "📈 Toplam grafiği", "callback_data": f"setgrf|{sid}"}])
    rows.append([{"text": "🗑 Seti sil", "callback_data": f"setsil|{sid}"}])
    rows.append(_nav("setler"))
    return "\n".join(satirlar), rows


def set_secim_gorunumu(p: dict) -> tuple[str, list]:
    """Kart → 📦 Sete ekle: mevcut setler (üyeyse çıkarma), yeni set kurma."""
    key = konfig.product_key(p)
    kid = kisa_id(key)
    rows = []
    for ad, s in konfig.setleri_getir().items():
        sid = kisa_id(ad)
        if key in s.get("urunler", []):
            rows.append([{"text": f"✓ {ad} — çıkar",
                          "callback_data": f"setcik|{kid}|{sid}"}])
        else:
            rows.append([{"text": f"📦 {ad} — ekle",
                          "callback_data": f"sete|{kid}|{sid}"}])
    rows.append([{"text": "➕ Yeni set kur", "callback_data": f"setyeni|{kid}"}])
    rows.append(_nav(f"kart|{kid}"))
    return f"📦 {p.get('label', '?')} hangi sete?", rows


def isim_ara(products: list[dict], sorgu: str) -> list[dict]:
    """Bulanık isim araması: tüm kelimeler etikette geçen ürünler."""
    kelimeler = [k for k in sorgu.casefold().split() if k]
    if not kelimeler:
        return []
    return [p for p in products
            if all(k in p.get("label", "").casefold() for k in kelimeler)]


# ===================== AKIŞLAR =====================

def liste_metni(products: list) -> str:
    if not products:
        return "İzlenen ürün yok. Eklemek için bana ürün linkini gönder."
    satirlar = []
    for i, p in enumerate(products, 1):
        thr = p.get("price_threshold_tl")
        hedef = f" — hedef {tl(float(thr))}" if thr else ""
        satirlar.append(f"{i}. {p.get('label', '?')}{hedef}")
    return "\n".join(satirlar)


def _urun_no(parca: list[str], products: list) -> tuple[dict | None, str]:
    """Eski numaralı komutlar için: '/sil 3' → ürün. (Yeni yol: /durum + butonlar)"""
    if len(parca) < 2 or not parca[1].isdigit():
        return None, ("Artık numara gerekmiyor: /durum yaz, ürüne dokun, "
                      "karttan butonla yap. (Eski kullanım: " + parca[0] + " <no>)")
    n = int(parca[1])
    if not 1 <= n <= len(products):
        return None, f"Geçersiz numara: {n} (1-{len(products)})."
    return products[n - 1], ""


async def urun_on_izleme(manager: WatcherManager, notifier: Notifier,
                         shared: dict, url: str) -> None:
    """Link geldi → sayfayı okur, adı/fiyatı çıkarır, hedefi butonla sordurur."""
    await notifier.send("🔎 Ürüne bakıyorum, 10-20 saniye...")
    try:
        s = await check_once(manager.context, manager.sites, manager.hizli,
                             {"label": "yeni ürün", "mode": "price"}, url)
    except Exception as e:
        await notifier.send(f"Sayfayı açamadım ({type(e).__name__}). "
                            "Linki kontrol edip tekrar gönder.")
        return
    label = konfig._tekil_etiket(
        konfig.baslik_temizle(s.get("title") or "") or konfig.etiket_uret(url),
        shared["products"])
    shared["bekleyen_urun"] = {"url": url, "fiyat": s["price"], "label": label}
    f = s["price"]
    if f and not s["blocked"]:
        rows = [[{"text": f"%3 altı → {tl(round(f * 0.97))}", "callback_data": "hedef|3"}],
                [{"text": f"%5 altı → {tl(round(f * 0.95))}", "callback_data": "hedef|5"}],
                [{"text": f"%10 altı → {tl(round(f * 0.90))}", "callback_data": "hedef|10"}],
                [{"text": "✍️ Kendim yazacağım", "callback_data": "elle"},
                 {"text": "❌ Vazgeç", "callback_data": "iptal"}]]
        ok = await notifier.send_buttons(
            f"🛒 {label}\n💰 Şu an: {tl(f)} ({s['host']})\n\nHedef fiyat ne olsun?", rows)
        if not ok:
            shared["bekleyen_urun"]["elle"] = True
            await notifier.send(f"🛒 {label} — şu an {tl(f)}.\n"
                                "Hedef fiyatı yaz (örn: 13500), vazgeçmek için 'iptal':")
    else:
        shared["bekleyen_urun"]["elle"] = True
        ek = " (site bot koruması gösterdi)" if s["blocked"] else ""
        await notifier.send(f"🛒 {label}\nFiyatı şu an okuyamadım{ek} — yine de "
                            "izlemeye alabilirim.\nHedef fiyatı yaz (örn: 13500), "
                            "vazgeçmek için 'iptal':")


async def urun_ekle_bitir(manager: WatcherManager, notifier: Notifier,
                          shared: dict, hedef: float) -> None:
    b = shared.pop("bekleyen_urun", None)
    if not b:
        return
    konfig.tg_urun_ekle(b["label"], b["url"], float(hedef))
    shared["products"] = konfig.load_products()
    manager.sync(shared["products"])
    kid = kisa_id(b["label"])
    # Ekleme biter bitmez en sık ihtiyaç: Akakçe'ye de bağlamak — otomatik öner
    await notifier.send_buttons(
        f"✅ Eklendi: {b['label']}\n🎯 Hedef: {tl(float(hedef))} — izleme başladı.\n\n"
        "Akakçe'de de arayayım mı? (tüm satıcıların en ucuzu tek sayfada)",
        [[{"text": "🔍 Evet, Akakçe'ye bağla", "callback_data": f"akk|{kid}"},
          {"text": "Şimdilik hayır", "callback_data": f"kart|{kid}"}]])


async def _akakce_akisi(manager: WatcherManager, notifier: Notifier, shared: dict,
                        p: dict, message_id=None) -> None:
    """Ürünü adıyla Akakçe'de arar, adayları butonla sunar."""
    key = konfig.product_key(p)
    kid = kisa_id(key)
    await notifier.send(f"🔎 Akakçe'de aranıyor: {p.get('label', '?')} (10-20 sn)...")
    adaylar = await akakce_ara(manager.context, manager.hizli, p.get("label", ""))
    if not adaylar:
        await notifier.send("Akakçe'de sonuç bulamadım. Ürünün Akakçe linkini "
                            "bulup bana göndersen de olur: karttan ➕ Kaynak ekle.")
        return
    shared.setdefault("akakce_aday", {})[kid] = adaylar
    rows = [[{"text": a["ad"][:60], "callback_data": f"akksec|{kid}|{i}"}]
            for i, a in enumerate(adaylar)]
    rows.append([{"text": "❌ Hiçbiri değil", "callback_data": f"kart|{kid}"}])
    await notifier.send_buttons(
        f"Akakçe'de bulduklarım — hangisi '{p.get('label', '?')}'?", rows)


# ===================== ANA DİNLEYİCİ =====================

def _gorev_sonucu_logla(gorev: asyncio.Task) -> None:
    if gorev.cancelled():
        return
    e = gorev.exception()
    if e is not None:
        logging.error(f"Telegram update işlenemedi: {type(e).__name__}: {e}")

async def telegram_listener(notifier: Notifier, shared: dict, state: State,
                            manager: WatcherManager, context: BrowserContext,
                            poll_rq=None) -> None:
    """Uzun sorgulamayla (getUpdates) komut + buton dinler. Sadece ayarlı
    chat_id'den gelenler işlenir; yabancı sohbetler yok sayılır.

    poll_rq: getUpdates için AYRI bağlantı bağlamı — hızlı buton/edit
    istekleriyle çakışmasın (yoklama takılınca butonlar cevapsız kalıyordu).
    Yoklama kısa tutulur (25 sn sunucu / 30 sn istemci): bağlantı stall olursa
    en geç ~30 sn'de bırakılıp yeniden yoklanır, bekleyen buton hemen alınır.
    Hatada 10 değil 2 sn beklenir → ölü pencere en aza iner."""
    if not notifier.token:
        return
    await notifier.tg("setMyCommands", commands=KOMUTLAR)   # "/" menüsü
    offset = 0
    while True:
        try:
            updates = await notifier.tg("getUpdates", timeout_ms=30000,
                                        rq=poll_rq, offset=offset, timeout=25)
            if updates is None:
                await asyncio.sleep(2)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                # Her update KENDİ görevinde işlenir: uzun süren bir işlem
                # (link önizleme, Akakçe araması) sonraki buton basışlarını
                # bekletmesin — arayüz asla donmasın.
                gorev = asyncio.create_task(
                    _update_isle(u, notifier, shared, state, manager, context))
                gorev.add_done_callback(_gorev_sonucu_logla)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.warning(f"Telegram dinleyici hatası: {e}")
            await asyncio.sleep(2)


async def _update_isle(u: dict, notifier: Notifier, shared: dict, state: State,
                       manager: WatcherManager, context: BrowserContext) -> None:
    cb = u.get("callback_query")
    if cb:
        await _callback_isle(cb, notifier, shared, state, manager, context)
        return

    msg = u.get("message") or {}
    chat = str((msg.get("chat") or {}).get("id", ""))
    text = (msg.get("text") or "").strip()
    if not text:
        return

    if not notifier.chat_id:
        # kurulum kolaylığı: chat_id ayarlı değilken /start'a id ile cevap ver
        if text.split()[0].lower().startswith("/start"):
            await notifier._send_telegram(
                f"chat_id'in: {chat}\nBunu products.yaml → telegram_chat_id "
                "alanına yaz ve botu yeniden başlat.", chat_id=chat)
        return
    if chat != notifier.chat_id:
        return  # yabancı sohbet — yok say

    if text.startswith("/"):
        await _komut_isle(text, notifier, shared, state, manager, context)
    else:
        await _mesaj_isle(text, notifier, shared, state, manager)


async def _mesaj_isle(text: str, notifier: Notifier, shared: dict,
                      state: State, manager: WatcherManager) -> None:
    """Düz metin: link → ekle/kaynak; sayı → beklenen hedef; metin → isim arama."""
    link = re.search(r"https?://\S+", text)

    bekleyen_kaynak = shared.get("bekleyen_kaynak")
    if link and bekleyen_kaynak:
        key = bekleyen_kaynak["key"]
        shared.pop("bekleyen_kaynak", None)
        url = link.group(0)
        konfig.tg_kaynak_ekle(key, url)
        shared["products"] = konfig.load_products()
        manager.sync(shared["products"], force={key})
        await notifier.send(f"✅ Kaynak eklendi: {key}\n🌐 {url}\n"
                            "Bot artık bu kaynağı da kontrol edip en ucuzunu bildirecek.")
        return
    if link:
        await urun_on_izleme(manager, notifier, shared, link.group(0))
        return

    if text.lower() in ("iptal", "vazgeç", "vazgec"):
        for k in ("bekleyen_urun", "bekleyen_hedef", "bekleyen_kaynak",
                  "bekleyen_set_hedef", "bekleyen_set_yeni"):
            shared.pop(k, None)
        await notifier.send("Vazgeçildi.")
        return

    # yeni set kurma: yazılan metin set adıdır
    bsy = shared.get("bekleyen_set_yeni")
    if bsy:
        ad = " ".join(text.split())[:40]
        shared.pop("bekleyen_set_yeni", None)
        konfig.set_urun(ad, bsy["key"])
        metin, rows = set_gorunumu(ad, shared["products"], state)
        await notifier.send_buttons(f"✅ 📦 '{ad}' kuruldu.\n\n" + metin, rows)
        return

    sayi = parse_try_amount(text)
    bekleyen_hedef = shared.get("bekleyen_hedef")

    bsh = shared.get("bekleyen_set_hedef")
    if sayi and bsh:
        ad = bsh["ad"]
        shared.pop("bekleyen_set_hedef", None)
        konfig.set_hedef(ad, sayi)
        metin, rows = set_gorunumu(ad, shared["products"], state)
        await notifier.send_buttons(
            f"🎯 '{ad}' set hedefi: {tl(sayi)} — toplam bu rakamın altına "
            "inince bildiririm.\n\n" + metin, rows)
        return

    # acil hedef kaldırma: '0'
    if bekleyen_hedef and bekleyen_hedef.get("acil") and text.strip() == "0":
        key = bekleyen_hedef["key"]
        shared.pop("bekleyen_hedef", None)
        konfig.tg_acil_hedef(key, None)
        shared["products"] = konfig.load_products()
        manager.sync(shared["products"], force={key})
        await notifier.send(f"🚨 {key} acil hedefi kaldırıldı.")
        return

    if sayi and bekleyen_hedef:
        key = bekleyen_hedef["key"]
        acil = bool(bekleyen_hedef.get("acil"))
        shared.pop("bekleyen_hedef", None)
        if acil:
            konfig.tg_acil_hedef(key, sayi)
        else:
            konfig.tg_hedef_degistir(key, sayi)
        _mute_temizle(state, key)
        shared["products"] = konfig.load_products()
        manager.sync(shared["products"], force={key})
        onek = "🚨 acil hedef" if acil else "🎯 yeni hedef"
        await notifier.send(f"{key} {onek}: {tl(sayi)}")
        return
    bekleyen = shared.get("bekleyen_urun")
    if sayi and bekleyen and bekleyen.get("elle"):
        await urun_ekle_bitir(manager, notifier, shared, sayi)
        return
    if bekleyen and bekleyen.get("elle"):
        await notifier.send("Anlayamadım — sadece rakam yaz (örn: 13500) ya da 'iptal'.")
        return

    # İsimle bulanık arama → tek eşleşme: kart; çok: seçim listesi
    bulunan = isim_ara(shared["products"], text)
    if len(bulunan) == 1:
        k = konfig.product_key(bulunan[0])
        metin, rows = kart_gorunumu(bulunan[0], state.get(k))
        await notifier.send_buttons(metin, rows)
    elif bulunan:
        rows = [[{"text": p.get("label", "?")[:50],
                  "callback_data": f"kart|{kisa_id(konfig.product_key(p))}"}]
                for p in bulunan[:8]]
        await notifier.send_buttons(f"'{text}' ile eşleşenler:", rows)
    else:
        await notifier.send(f"'{text}' adında ürün bulamadım.\n\n" + YARDIM)


async def _komut_isle(text: str, notifier: Notifier, shared: dict, state: State,
                      manager: WatcherManager, context: BrowserContext) -> None:
    products = shared["products"]
    parca = text.split()
    cmd = parca[0].lower().split("@")[0]

    if cmd in ("/start", "/menu", "/basla"):
        metin, rows = ana_menu_gorunumu(products, state)
        await notifier.send_buttons(metin, rows)

    elif cmd in ("/yardim", "/help"):
        await notifier.send_buttons(YARDIM, [_nav(None)])

    elif cmd in ("/durum", "/liste"):
        metin, rows = durum_gorunumu(products, state)
        await notifier.send_buttons(metin, rows)

    elif cmd == "/sorunlu":
        metin, rows = durum_gorunumu(products, state, sadece_sorunlu=True)
        await notifier.send_buttons(metin, rows)

    elif cmd in ("/setler", "/set"):
        metin, rows = setler_gorunumu(state)
        await notifier.send_buttons(metin, rows)

    elif cmd in ("/saglik", "/sağlık"):
        await notifier.send_buttons(saglik_ozeti(products, state),
                                    [[{"text": "🗄 Şimdi yedekle",
                                       "callback_data": "yedekle"}], _nav(None)])

    elif cmd == "/yedek":
        ok = await yedek_al(notifier, state)
        if not ok:
            await notifier.send("⚠️ Yedeklenecek veri yok.")

    elif cmd == "/ekle":
        url = parca[1] if len(parca) > 1 else ""
        if not url.startswith("http"):
            await notifier.send("Bana ürün linkini göndermen yeterli — komutsuz da olur.")
            return
        hedef = parse_try_amount(parca[2]) if len(parca) > 2 else None
        if not hedef:
            await urun_on_izleme(manager, notifier, shared, url)
            return
        label = konfig._tekil_etiket(" ".join(parca[3:]).strip()
                                     or konfig.etiket_uret(url), products)
        konfig.tg_urun_ekle(label, url, hedef)
        shared["products"] = konfig.load_products()
        manager.sync(shared["products"])
        await notifier.send(f"✅ Eklendi: {label}\n🎯 Hedef: {tl(hedef)} — izleme başladı.")

    elif cmd == "/sil":                      # eski yol — çalışmaya devam eder
        p, hata = _urun_no(parca, products)
        if p is None:
            await notifier.send(hata)
            return
        await _sil_onayi(notifier, p)

    elif cmd == "/hedef":                    # eski yol
        p, hata = _urun_no(parca, products)
        yeni = parse_try_amount(parca[2]) if len(parca) > 2 else None
        if p is None or not yeni:
            await notifier.send(hata or "Kullanım: /hedef <no> <fiyatTL> — ya da "
                                        "/durum'dan ürüne dokunup 🎯 butonunu kullan.")
            return
        key = konfig.product_key(p)
        konfig.tg_hedef_degistir(key, yeni)
        _mute_temizle(state, key)
        shared["products"] = konfig.load_products()
        manager.sync(shared["products"], force={key})
        await notifier.send(f"🎯 {p.get('label', '?')} yeni hedef: {tl(yeni)}")

    elif cmd == "/akakce":                   # eski yol
        p, hata = _urun_no(parca, products)
        if p is None:
            await notifier.send(hata)
            return
        await _akakce_akisi(manager, notifier, shared, p)

    elif cmd == "/csv":
        disari = konfig.BASE_DIR / "fiyat_gecmisi_export.csv"
        n = veri.csv_disari_aktar(disari)
        if n:
            await notifier.send_document(disari, f"Ham fiyat geçmişi ({n} kayıt)")
        else:
            await notifier.send("Henüz fiyat kaydı yok.")

    elif cmd == "/grafik":
        try:
            png = await grafik_png(context)
            await notifier.send_photo(png, "Fiyat grafiği")
            await notifier.send_document(
                konfig.GRAFIK_HTML, "Etkileşimli sürüm — indirip tarayıcıda aç")
        except Exception as e:
            await notifier.send(f"Grafik üretilemedi: {e}")

    else:
        await notifier.send("Bu komutu bilmiyorum.\n\n" + YARDIM)


async def _sil_onayi(notifier: Notifier, p: dict) -> None:
    kid = kisa_id(konfig.product_key(p))
    await notifier.send_buttons(
        f"🗑 '{p.get('label', '?')}' izlemeden çıkarılsın mı?",
        [[{"text": "Evet, sil", "callback_data": f"silon|{kid}"},
          {"text": "Vazgeç", "callback_data": f"kart|{kid}"}]])


# ===================== BUTON (CALLBACK) İŞLEME =====================

async def _callback_isle(cb: dict, notifier: Notifier, shared: dict, state: State,
                         manager: WatcherManager, context: BrowserContext) -> None:
    cb_chat = str(((cb.get("message") or {}).get("chat") or {}).get("id", ""))
    await notifier.tg("answerCallbackQuery", callback_query_id=cb.get("id"))
    if not notifier.chat_id or cb_chat != notifier.chat_id:
        return
    data = cb.get("data", "")
    mid = (cb.get("message") or {}).get("message_id")
    parca = data.split("|")
    islem = parca[0]
    products = shared["products"]

    async def _kart_guncelle(p):
        k = konfig.product_key(p)
        metin, rows = kart_gorunumu(p, state.get(k), _kart_geri(shared, kisa_id(k)))
        await notifier.edit_buttons(mid, metin, rows)

    # --- ekleme akışının eski callback'leri ---
    bekleyen = shared.get("bekleyen_urun")
    if islem == "iptal":
        for k in ("bekleyen_urun", "bekleyen_hedef", "bekleyen_kaynak"):
            shared.pop(k, None)
        await notifier.send("Vazgeçildi.")
        return
    if islem == "elle" and bekleyen:
        bekleyen["elle"] = True
        await notifier.send("Hedef fiyatı yaz (örn: 13500), vazgeçmek için 'iptal':")
        return
    if islem == "hedef" and bekleyen and bekleyen.get("fiyat"):
        yuzde = float(parca[1])
        await urun_ekle_bitir(manager, notifier, shared,
                              round(bekleyen["fiyat"] * (1 - yuzde / 100)))
        return

    # --- ana menü + yardım ---
    if islem == "menu":
        metin, rows = ana_menu_gorunumu(products, state)
        await notifier.edit_buttons(mid, metin, rows)
        return
    if islem == "yardim":
        await notifier.edit_buttons(mid, YARDIM, [_nav(None)])
        return
    if islem == "saglik":
        await notifier.edit_buttons(mid, saglik_ozeti(products, state),
                                    [[{"text": "🗄 Şimdi yedekle",
                                       "callback_data": "yedekle"}], _nav(None)])
        return
    if islem == "yedekle":
        await notifier.edit_buttons(mid, "🗄 Yedek alınıyor...", [_nav(None)])
        ok = await yedek_al(notifier, state)
        await notifier.send("✅ Yedek gönderildi." if ok
                            else "⚠️ Yedeklenecek veri yok.")
        return

    # --- liste görünümleri ---
    if islem in ("d", "sor"):
        sayfa = int(parca[1]) if len(parca) > 1 else 0
        metin, rows = durum_gorunumu(products, state, sayfa,
                                     sadece_sorunlu=(islem == "sor"))
        await notifier.edit_buttons(mid, metin, rows)
        return
    if islem == "grftum":
        try:
            png = await grafik_png(context)
            await notifier.send_photo(png, "Fiyat grafiği")
        except Exception as e:
            await notifier.send(f"Grafik üretilemedi: {e}")
        return

    # --- set işlemleri (ürün korumasından bağımsız) ---
    if islem == "setler":
        metin, rows = setler_gorunumu(state)
        await notifier.edit_buttons(mid, metin, rows)
        return
    if islem in ("set", "sethedef", "setgrf", "setsil", "setsilon"):
        ad = set_bul(parca[1]) if len(parca) > 1 else None
        if ad is None:
            await notifier.edit_buttons(mid, "Bu set artık yok.",
                                        [_nav("d|0")])
            return
        sid = kisa_id(ad)
        if islem == "set":
            metin, rows = set_gorunumu(ad, products, state)
            await notifier.edit_buttons(mid, metin, rows)
        elif islem == "sethedef":
            shared["bekleyen_set_hedef"] = {"ad": ad}
            await notifier.send(f"🎯 '{ad}' seti için TOPLAM hedef fiyatı yaz "
                                "(örn: 84000), vazgeçmek için 'iptal':")
        elif islem == "setgrf":
            try:
                import grafik
                s = konfig.setleri_getir()[ad]
                seri = veri.set_toplam_serisi(s.get("urunler", []))
                html = grafik.generate_set(ad, seri, s.get("hedef"))
                png = await png_cek(context, html)
                await notifier.send_photo(png, f"📦 {ad} — set toplamı")
            except Exception as e:
                await notifier.send(f"Set grafiği üretilemedi: {e}")
        elif islem == "setsil":
            await notifier.edit_buttons(
                mid, f"🗑 '{ad}' seti silinsin mi? (ürünler silinmez, "
                     "sadece gruplama kalkar)",
                [[{"text": "Evet, sil", "callback_data": f"setsilon|{sid}"},
                  {"text": "Vazgeç", "callback_data": f"set|{sid}"}]])
        elif islem == "setsilon":
            konfig.set_sil(ad)
            metin, rows = setler_gorunumu(state)
            await notifier.edit_buttons(mid, f"🗑 '{ad}' seti silindi.\n\n" + metin, rows)
        return

    # --- ürün bazlı işlemler (kid ile) ---
    kid = parca[1] if len(parca) > 1 else ""

    # Geri al, ürün LİSTEDEN SİLİNMİŞKEN çalışmak zorunda → korumadan önce
    if islem == "geria":
        kayit = (shared.get("son_silinen") or {}).pop(kid, None)
        if not kayit:
            await notifier.send("Geri alamadım — ürünü linkiyle tekrar ekleyebilirsin.")
            return
        if kayit.get("ham"):
            konfig.tg_urun_ekle_ham(kayit["ham"])
        konfig.tg_urun_geri_al(kayit["key"])
        shared["products"] = konfig.load_products()
        manager.sync(shared["products"])
        p2 = urun_bul(shared["products"], kid)
        if p2 is None:
            await notifier.send("Geri alamadım — ürünü linkiyle tekrar ekleyebilirsin.")
            return
        metin, rows = kart_gorunumu(p2, state.get(kayit["key"]))
        await notifier.edit_buttons(mid, "↩️ Geri alındı — izleme sürüyor.\n\n" + metin, rows)
        return

    p = urun_bul(products, kid)
    if p is None:
        await notifier.edit_buttons(mid, "Bu ürün artık listede yok.",
                                    [_nav("d|0")])
        return
    key = konfig.product_key(p)

    if islem == "kart":
        if len(parca) >= 3:      # nereden gelindi → bağlamsal geri için sakla
            shared.setdefault("kart_ori", {})[kid] = parca[2]
        await _kart_guncelle(p)

    elif islem == "hedefsec":
        metin, rows = hedef_secim_gorunumu(p, state.get(key))
        await notifier.edit_buttons(mid, metin, rows)

    elif islem == "hpct":
        fp = state.get(key).get("last_good_price")
        if not fp:
            await notifier.send("Güncel fiyat yok — hedefi elle yaz:")
            shared["bekleyen_hedef"] = {"key": key}
            return
        yeni = round(fp * (1 - float(parca[2]) / 100))
        konfig.tg_hedef_degistir(key, float(yeni))
        _mute_temizle(state, key)
        shared["products"] = konfig.load_products()
        manager.sync(shared["products"], force={key})
        p2 = urun_bul(shared["products"], kid) or p
        await _kart_guncelle(p2)

    elif islem == "helle":
        shared["bekleyen_hedef"] = {"key": key}
        await notifier.send(f"🎯 {p.get('label', '?')} için yeni hedefi yaz "
                            "(örn: 13500), vazgeçmek için 'iptal':")

    elif islem == "hacil":
        shared["bekleyen_hedef"] = {"key": key, "acil": True}
        await notifier.send(
            f"🚨 {p.get('label', '?')} için ACİL hedefi yaz (örn: 12000).\n"
            "Bu eşiğin altında: cooldown beklenmez, sessiz saati deler.\n"
            "Kaldırmak için '0', vazgeçmek için 'iptal':")

    elif islem == "setsec":
        metin, rows = set_secim_gorunumu(p)
        await notifier.edit_buttons(mid, metin, rows)

    elif islem in ("sete", "setcik"):
        ad = set_bul(parca[2]) if len(parca) > 2 else None
        if ad is None:
            await notifier.send("Bu set artık yok.")
            return
        konfig.set_urun(ad, key, ekle=(islem == "sete"))
        if islem == "sete":
            metin, rows = set_gorunumu(ad, products, state)
            await notifier.edit_buttons(
                mid, f"✅ '{p.get('label', '?')}' → 📦 {ad}\n\n" + metin, rows)
        else:
            await _kart_guncelle(p)

    elif islem == "setyeni":
        shared["bekleyen_set_yeni"] = {"key": key}
        await notifier.send(f"📦 Yeni set adı yaz (örn: PC Toplama) — "
                            f"'{p.get('label', '?')}' ilk üye olacak. "
                            "Vazgeçmek için 'iptal':")

    elif islem == "akk":
        await _akakce_akisi(manager, notifier, shared, p, mid)

    elif islem == "akksec":
        adaylar = (shared.get("akakce_aday") or {}).get(kid) or []
        i = int(parca[2])
        if not 0 <= i < len(adaylar):
            await notifier.send("Seçim zaman aşımına uğradı — karttan tekrar dene.")
            return
        konfig.tg_kaynak_ekle(key, adaylar[i]["url"])
        shared.get("akakce_aday", {}).pop(kid, None)
        shared["products"] = konfig.load_products()
        manager.sync(shared["products"], force={key})
        p2 = urun_bul(shared["products"], kid) or p
        metin, rows = kart_gorunumu(p2, state.get(key))
        await notifier.edit_buttons(
            mid, "✅ Akakçe birincil kaynak oldu (tüm satıcıların en ucuzu).\n\n" + metin, rows)

    elif islem == "kaynak":
        shared["bekleyen_kaynak"] = {"key": key}
        await notifier.send(f"➕ {p.get('label', '?')} için yeni kaynak linkini "
                            "gönder (başka mağaza ya da Akakçe), 'iptal' ile vazgeç:")

    elif islem in ("durdur", "devam"):
        konfig.tg_duraklat(key, islem == "durdur")
        shared["products"] = konfig.load_products()
        manager.sync(shared["products"], force={key})
        p2 = urun_bul(shared["products"], kid) or p
        await _kart_guncelle(p2)

    elif islem == "sil":
        await notifier.edit_buttons(
            mid, f"🗑 '{p.get('label', '?')}' izlemeden çıkarılsın mı?",
            [[{"text": "Evet, sil", "callback_data": f"silon|{kid}"},
              {"text": "Vazgeç", "callback_data": f"kart|{kid}"}]])

    elif islem in ("silon", "aldim"):
        # Telegram'dan eklenen ürünse kaydı sakla ki 'geri al' aynen dönebilsin
        tg = konfig._tg_dosya()
        ham = next((q for q in tg["eklenen"] if konfig.product_key(q) == key), None)
        shared.setdefault("son_silinen", {})[kid] = {"key": key, "ham": ham}
        konfig.tg_urun_sil(key)
        shared["products"] = konfig.load_products()
        manager.sync(shared["products"])
        on_ek = "✅ Alındı olarak işaretlendi, izleme bırakıldı" \
            if islem == "aldim" else "🗑 İzlemeden çıkarıldı"
        await notifier.edit_buttons(
            mid, f"{on_ek}: {p.get('label', '?')}",
            [[{"text": "↩️ Geri al", "callback_data": f"geria|{kid}"}], _nav("d|0")])

    elif islem == "sustur":
        st = state.get(key)
        st["mute_until"] = time.time() + 7 * 24 * 3600
        await state.save()
        await notifier.send(f"🔕 {p.get('label', '?')} alarmları 1 hafta susturuldu.\n"
                            "(İzleme sürer, sadece hedef alarmı gelmez. Açmak: karttan 🎯)")

    elif islem == "grf":
        try:
            png = await grafik_png(context, urun=p.get("label"))
            await notifier.send_photo(png, f"📈 {p.get('label', '?')}")
        except Exception as e:
            await notifier.send(f"Grafik üretilemedi: {e}")
