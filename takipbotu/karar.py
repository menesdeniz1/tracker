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
    ucuz, veya son iyi fiyata göre %40'tan fazla ani düşüş → önce doğrula, sonra
    bildir. Sadece hedef alarmını değil, 30-gün-dibi sinyalini ve state'e yazılan
    fiyatı da korur (bozuk fiyat daily_min'e girerse 35 gün gerçek dibi maskeler)."""
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
    return False


def en_iyi_kaynak(prod: dict, sonuclar: list[dict]) -> dict | None:
    """Kaynaklar arasından bildirime esas olanı seçer: stok modunda stoklu olan,
    fiyat modunda EN UCUZ fiyatlı olan (Akakçe birincil kurgusunun kalbi)."""
    adaylar = [s for s in sonuclar if not s["blocked"]]
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
