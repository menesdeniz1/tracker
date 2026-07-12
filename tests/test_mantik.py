# -*- coding: utf-8 -*-
"""Karar mantığı: alarm koşulları, şüpheli fiyat koruması, en ucuz kaynak
seçimi, 30-gün-dibi hesapları, etiket yardımcıları."""
from datetime import date, timedelta

from takipbotu.karar import alarm_gerekli, asiri_supheli, en_iyi_kaynak, fiyat_suphali
from takipbotu.konfig import _tekil_etiket, baslik_temizle, etiket_uret, product_key, product_urls
from takipbotu.veri import dip30_oncesi, gunluk_min_guncelle, yedi_gun_degisim


def _sonuc(**kw):
    s = {"url": "u", "host": "site.com", "price": None, "source": "seçici",
         "seller": None, "title": "", "in_stock": None, "variant_ok": True,
         "variant": "AUTO", "blocked": False}
    s.update(kw)
    return s


# ---------- alarm_gerekli ----------

def test_alarm_fiyat_hedefin_altinda():
    prod = {"mode": "price", "price_threshold_tl": 10000}
    gerekli, _ = alarm_gerekli(prod, _sonuc(price=9500))
    assert gerekli


def test_alarm_fiyat_hedefin_ustunde():
    prod = {"mode": "price", "price_threshold_tl": 10000}
    gerekli, _ = alarm_gerekli(prod, _sonuc(price=10500))
    assert not gerekli


def test_alarm_stok_modu():
    prod = {"mode": "stock"}
    assert alarm_gerekli(prod, _sonuc(in_stock=True))[0]
    assert not alarm_gerekli(prod, _sonuc(in_stock=False))[0]
    assert not alarm_gerekli(prod, _sonuc(in_stock=True, variant_ok=False))[0]


def test_alarm_fiyat_ve_stok():
    prod = {"mode": "price+stock", "price_threshold_tl": 10000}
    assert alarm_gerekli(prod, _sonuc(price=9000, in_stock=True))[0]
    assert not alarm_gerekli(prod, _sonuc(price=9000, in_stock=False))[0]
    assert not alarm_gerekli(prod, _sonuc(price=11000, in_stock=True))[0]


# ---------- fiyat_suphali (sahte fiyat koruması) ----------

def test_supheli_regex_gecmis_yoksa():
    assert fiyat_suphali({}, _sonuc(price=9500, source="regex"), {})


def test_supheli_regex_son_iyiye_yakinsa_temiz():
    st = {"last_good_price": 10000}
    assert not fiyat_suphali({}, _sonuc(price=9800, source="regex"), st)


def test_supheli_regex_sapiyorsa():
    st = {"last_good_price": 10000}
    assert fiyat_suphali({}, _sonuc(price=8000, source="regex"), st)


def test_supheli_hedefin_yarisindan_ucuz():
    prod = {"price_threshold_tl": 9000}
    assert fiyat_suphali(prod, _sonuc(price=4000), {"last_good_price": 10000})


def test_supheli_ani_dusus():
    st = {"last_good_price": 10000}
    assert fiyat_suphali({}, _sonuc(price=5500), st)      # %45 düşüş
    assert not fiyat_suphali({}, _sonuc(price=9500), st)  # normal


# ---------- asiri_supheli (kalıcı-bozuk-kaynak koruması) ----------

def test_asiri_supheli_dusuk_gercek_vaka():
    """Gerçek vaka: n11 kategori sayfasına düşmüş, hedefin (12500) çok altında
    (1260) regex fiyatı — bu ASLA 2-okuma tutarlılığıyla doğrulanmamalı."""
    prod = {"price_threshold_tl": 12500}
    assert asiri_supheli(prod, _sonuc(price=1260, source="regex"), {})


def test_asiri_supheli_dusuk_esik_siniri():
    prod = {"price_threshold_tl": 10000}
    assert asiri_supheli(prod, _sonuc(price=1999, source="regex"), {})      # %20'nin altı
    assert not asiri_supheli(prod, _sonuc(price=2500, source="regex"), {})  # tam %25 — degil


def test_asiri_supheli_dusuk_sadece_regex_kaynaginda():
    """Aynı düşük fiyat json-ld/seçici gibi güvenilir kaynaktan gelirse
    'aşırı düşük regex' değildir — gerçek bir fırsat olabilir."""
    prod = {"price_threshold_tl": 12500}
    assert not asiri_supheli(prod, _sonuc(price=1260, source="json-ld"), {})
    assert not asiri_supheli(prod, _sonuc(price=1260, source="seçici"), {})


def test_asiri_supheli_dusuk_hedefsiz_urunde_tetiklenmez():
    assert not asiri_supheli({}, _sonuc(price=100, source="regex"), {})


def test_asiri_supheli_dusuk_fiyatsizda_tetiklenmez():
    prod = {"price_threshold_tl": 10000}
    assert not asiri_supheli(prod, _sonuc(price=None, source="regex"), {})


def test_asiri_supheli_yuksek_gercek_vaka():
    """Gerçek vaka: Akakçe'de tek bir pazaryeri satıcısı ('TRENDYPAZARS')
    ürünü gerçek fiyatının katları ile listelemişti — json-ld (GÜVENİLİR
    kaynak) olmasına rağmen son iyi fiyatın 3 katından fazlaysa şüpheli."""
    st = {"last_good_price": 55499.0}
    assert asiri_supheli({}, _sonuc(price=484424.80, source="json-ld"), st)  # ~8.7x
    assert asiri_supheli({}, _sonuc(price=119567.84, source="json-ld"),
                         {"last_good_price": 10495.0})                       # ~11.4x


def test_asiri_supheli_yuksek_esik_siniri():
    st = {"last_good_price": 10000}
    assert asiri_supheli({}, _sonuc(price=30001, source="json-ld"), st)      # 3x'in üstü
    assert not asiri_supheli({}, _sonuc(price=18000, source="json-ld"), st)  # normal şüpheli ama asiri değil


def test_asiri_supheli_yuksek_gecmis_yoksa_tetiklenmez():
    """İlk okumada (last_good_price yok) aşırı-yüksek kontrolü uygulanamaz —
    karşılaştıracak bir geçmiş olmalı."""
    assert not asiri_supheli({}, _sonuc(price=999999, source="json-ld"), {})


# ---------- en_iyi_kaynak (Akakçe birincil kurgusunun kalbi) ----------

def test_en_ucuz_kaynak_secilir():
    sonuclar = [_sonuc(price=100, host="a"), _sonuc(price=90, host="b"),
                _sonuc(price=95, host="c")]
    assert en_iyi_kaynak({"mode": "price"}, sonuclar)["host"] == "b"


def test_engelli_kaynak_elenir():
    sonuclar = [_sonuc(price=50, blocked=True), _sonuc(price=90)]
    assert en_iyi_kaynak({"mode": "price"}, sonuclar)["price"] == 90


def test_hepsi_engelliyse_none():
    assert en_iyi_kaynak({"mode": "price"}, [_sonuc(blocked=True)]) is None


def test_stok_modunda_stoklu_kaynak_oncelikli():
    sonuclar = [_sonuc(price=90, in_stock=False),
                _sonuc(price=100, in_stock=True)]
    assert en_iyi_kaynak({"mode": "stock"}, sonuclar)["in_stock"] is True


# ---------- 30-gün dibi + 7 günlük trend ----------

def _gun(kac_gun_once):
    return (date.today() - timedelta(days=kac_gun_once)).isoformat()


def test_gunluk_min_guncelle_ve_budama():
    st = {"daily_min": {_gun(40): 50.0}}       # 35 günden eski → budanmalı
    gunluk_min_guncelle(st, 100.0)
    gunluk_min_guncelle(st, 80.0)              # aynı günün daha düşüğü kazanır
    assert st["daily_min"][_gun(0)] == 80.0
    assert _gun(40) not in st["daily_min"]


def test_dip30_bugunu_haric_tutar():
    st = {"daily_min": {_gun(0): 70.0, _gun(3): 100.0, _gun(10): 95.0}}
    dip, gun_sayisi = dip30_oncesi(st)
    assert dip == 95.0 and gun_sayisi == 2


def test_dip30_veri_yoksa():
    assert dip30_oncesi({}) == (None, 0)


def test_yedi_gun_degisim():
    st = {"last_good_price": 95.0, "daily_min": {_gun(7): 100.0}}
    assert round(yedi_gun_degisim(st), 2) == -5.0


def test_yedi_gun_veri_yoksa_none():
    assert yedi_gun_degisim({"last_good_price": 95.0}) is None
    assert yedi_gun_degisim({"last_good_price": 95.0,
                             "daily_min": {_gun(20): 100.0}}) is None


# ---------- etiket / URL yardımcıları ----------

def test_baslik_temizle_site_adini_kirpar():
    assert baslik_temizle("AMD Ryzen 7 7800X3D | Amazon.com.tr") == "AMD Ryzen 7 7800X3D"
    assert baslik_temizle("Ürün - Hepsiburada") == "Ürün"
    assert len(baslik_temizle("ç" * 200)) <= 60


def test_etiket_uret_urlden():
    ad = etiket_uret("https://www.akakce.com/islemci/en-ucuz-ryzen-7-fiyati,123.html")
    assert "fiyati" in ad and "," not in ad and len(ad) <= 48


def test_tekil_etiket_cakismada_numaralanir():
    mevcut = [{"label": "X"}, {"label": "X (2)"}]
    assert _tekil_etiket("X", mevcut) == "X (3)"
    assert _tekil_etiket("Y", mevcut) == "Y"


def test_product_key_ve_urls():
    p1 = {"label": "A", "url": "http://x"}
    p2 = {"urls": ["http://akakce", "http://magaza"]}
    assert product_key(p1) == "A"
    assert product_urls(p1) == ["http://x"]
    assert product_urls(p2)[0] == "http://akakce"   # sıra korunur (Akakçe birincil)
    assert product_key(p2) == "http://akakce"       # etiketsizse ilk URL
