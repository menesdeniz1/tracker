# -*- coding: utf-8 -*-
"""Karar mantığı: alarm gerekli mi, fiyat şüpheli mi, hangi kaynak esas."""
from .fiyat import tl


def alarm_gerekli(prod: dict, s: dict) -> tuple[bool, str]:
    mode = prod.get("mode", "price")
    thr = float(prod.get("price_threshold_tl", 0))
    if mode == "price":
        if s["price"] is not None and s["price"] <= thr:
            return True, f"💰 {tl(s['price'])} (hedef {tl(thr)})"
        return False, ""
    if mode == "stock":
        if s["variant_ok"] and s["in_stock"]:
            return True, f"📦 STOKTA — beden: {s['variant']}"
        return False, ""
    # price+stock
    if (s["variant_ok"] and s["in_stock"]
            and s["price"] is not None and s["price"] <= thr):
        return True, (f"📦 STOKTA + 💰 {tl(s['price'])} (hedef {tl(thr)}) "
                      f"— beden: {s['variant']}")
    return False, ""


def fiyat_suphali(prod: dict, s: dict, st: dict) -> bool:
    """Parse hatası ihtimali: doğrulanmamış regex okuması, hedefin yarısından da
    ucuz, ani düşüş, VEYA ani anormal YÜKSELİŞ → önce doğrula, sonra bildir.
    Sadece hedef alarmını değil, 30-gün-dibi sinyalini ve state'e yazılan
    fiyatı da korur (bozuk fiyat daily_min'e girerse 35 gün gerçek dibi maskeler).

    Yüksek-uçlu kontrol gerçek bir vakadan geliyor: Akakçe'nin kendi sayfasında
    (json-ld — YÜKSEK GÜVEN kabul edilen kaynak) tek bir pazaryeri satıcısı
    ("en ucuz" olarak öne çıkan ama fiyatı hatalı girilmiş biri) bir ürünü
    gerçek değerinin 2-9 katı fiyatla listeleyebiliyor; Akakçe bunu "en ucuz"
    diye gösteriyor. Kaynak güvenilir OLDUĞU için eskiden hiç yakalanmıyordu —
    ani düşüş kontrolüyle simetrik olarak ani yükselişi de şüpheli sayıyoruz."""
    fp = s["price"]
    if fp is None:
        return False
    son_iyi = st.get("last_good_price")
    if s["source"] == "regex":
        # Son iyi fiyata ±%5 yakınsa geçmiş bu okumayı doğruluyor demektir;
        # sapıyorsa (veya ilk okumaysa) ikinci okuma şart. Böylece sürekli
        # regex'te kalan ürünler her turda yeniden doğrulanmaz.
        if not son_iyi or abs(fp - son_iyi) > son_iyi * 0.05:
            return True
    thr = float(prod.get("price_threshold_tl", 0))
    if thr and fp < thr * 0.5:
        return True
    if son_iyi and fp < son_iyi * 0.6:
        return True
    if son_iyi and fp > son_iyi * 1.8:
        return True
    return False


def asiri_supheli(prod: dict, s: dict, st: dict) -> bool:
    """Kaynağın YAPISAL OLARAK bozuk (yanlış sayfa ya da hatalı tekil
    pazaryeri listesi) olduğunu gösteren AŞIRI sapmalar. Bu durumda normal
    2-okuma tutarlılığı GEÇERSİZ sayılır — çünkü bozuk bir kaynak, DÜZELENE
    KADAR kendisiyle saatlerce/günlerce 'tutarlı' kalabilir; iki ardışık
    okumanın örtüşmesi doğruluk kanıtı değildir.

    İki gerçek vaka bu korumayı doğurdu:
      • DÜŞÜK: n11 linki genel 'Hard Disk' kategori sayfasına düşmüştü; regex
        oradan hedefin (12.500 TL) çok altında sabit 1.260 TL okuyordu — sayfa
        hep aynı yanlış değeri döndürdüğü için 'tutarlı' sayılıp iki kez
        yanlış alarm gönderildi.
      • YÜKSEK: Akakçe'nin KENDİ sayfasında (json-ld — güvenilir kaynak
        sayılır) tek bir pazaryeri satıcısı ürünü gerçek değerinin 2-11 katı
        fiyatla listelemişti; Akakçe bunu saatlerce 'en ucuz' gösterdi.
        Kaynak güvenilir olsa da veri hatalıydı.

    Sadece güvenilir bir kaynaktan gelen makul bir okuma ya da fiyatın
    normal aralığa dönmesi bu durumu temizleyebilir."""
    fp = s["price"]
    if fp is None:
        return False
    thr = float(prod.get("price_threshold_tl", 0))
    if s["source"] == "regex" and thr and fp < thr * 0.2:
        return True
    son_iyi = st.get("last_good_price")
    if son_iyi and fp > son_iyi * 3:
        return True
    return False


def en_iyi_kaynak(prod: dict, sonuclar: list[dict]) -> dict | None:
    """Kaynaklar arasından bildirime esas olanı seçer: stok modunda stoklu olan,
    fiyat modunda EN UCUZ fiyatlı olan (Akakçe birincil kurgusunun kalbi)."""
    adaylar = [s for s in sonuclar if not s["blocked"] and not s.get("dead")]
    if not adaylar:
        return None
    mode = prod.get("mode", "price")
    if "stock" in mode:
        stoklu = [s for s in adaylar if s["variant_ok"] and s["in_stock"]]
        havuz = stoklu or adaylar
    else:
        havuz = adaylar
    fiyatli = [s for s in havuz if s["price"] is not None]
    if fiyatli:
        return min(fiyatli, key=lambda s: s["price"])
    return havuz[0]
