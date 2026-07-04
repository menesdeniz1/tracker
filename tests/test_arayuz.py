# -*- coding: utf-8 -*-
"""Kart/buton arayüzü: görünüm kurucular (saf) + buton akışları (sahte
notifier/manager ile). Telegram'a hiç çıkmadan tüm dispatch test edilir."""
import asyncio
import time

import pytest
import yaml

from takipbotu import arayuz, konfig, veri
from takipbotu.arayuz import (
    durum_gorunumu,
    hedef_secim_gorunumu,
    isim_ara,
    kart_gorunumu,
    kisa_id,
    sorun_metni,
    sparkline,
    urun_satiri,
)

# ===================== SAF GÖRÜNÜM KURUCULAR =====================

def test_kisa_id_kalici_ve_kisa():
    a = kisa_id("AMD Ryzen 7 7800X3D")
    assert a == kisa_id("AMD Ryzen 7 7800X3D")   # deterministik
    assert len(a) == 10
    assert a != kisa_id("Başka Ürün")


def test_sorun_metni():
    assert sorun_metni({}, {}) == "hiç okunamadı"
    assert sorun_metni({}, {"last_good_ts": time.time()}) is None
    eski = {"last_good_ts": time.time() - 3 * 24 * 3600}
    assert "gündür" in sorun_metni({}, eski)
    assert sorun_metni({"paused": True}, {}) is None     # duraklatma sorun değil


def test_urun_satiri_iki_sutun():
    """Sol sütun ürün adı, sağ sütun fiyat/durum — fiyat telefonda hep görünür."""
    sol, sag = urun_satiri({"label": "X", "paused": True}, {})
    assert sag["text"].startswith("⏸")
    sol, sag = urun_satiri({"label": "X"}, {})
    assert sag["text"].startswith("⚠️") and "hiç" in sag["text"]
    st = {"last_good_price": 90.0, "last_good_ts": time.time()}
    sol, sag = urun_satiri({"label": "Uzun Bir Ürün Adı", "price_threshold_tl": 100}, st)
    assert sol["text"].startswith("Uzun Bir")
    assert sag["text"] == "🔥 90₺"                    # hedefte + kompakt fiyat
    st2 = {"last_good_price": 67204.0, "last_good_ts": time.time()}
    _, sag = urun_satiri({"label": "X", "price_threshold_tl": 62000}, st2)
    assert sag["text"] == "67.204₺→62.000₺"   # yüzde değil, somut hedef fiyat
    # iki buton da ayni karti acar
    assert sol["callback_data"] == urun_satiri({"label": "Uzun Bir Ürün Adı"}, st)[1]["callback_data"]


def test_urun_satiri_sorunlu_yas():
    eski = {"last_good_ts": time.time() - 3 * 24 * 3600}
    _, sag = urun_satiri({"label": "X"}, eski)
    assert "3g" in sag["text"]


def _state(tmp_path, data):
    st = veri.State(tmp_path / "state.json")
    st.data = data
    return st


def test_durum_gorunumu_sorunlular_ustte(tmp_path):
    simdi = time.time()
    products = [{"label": "Saglam", "price_threshold_tl": 100},
                {"label": "Bozuk"},
                {"label": "Durgun", "paused": True}]
    state = _state(tmp_path, {"Saglam": {"last_good_price": 110.0,
                                         "last_good_ts": simdi}})
    metin, rows = durum_gorunumu(products, state)
    assert "3 ürün" in metin and "⚠️ 1" in metin and "⏸ 1" in metin
    # ilk ürün satırı sorunlu olan olmalı, duraklatılan en sonda
    # (durum artık SAĞ sütunda: [ad][fiyat/durum])
    sag_sutun = [r[1]["text"] for r in rows
                 if r[0].get("callback_data", "").startswith("kart|")]
    assert sag_sutun[0].startswith("⚠️")
    assert sag_sutun[-1].startswith("⏸")


def test_durum_gorunumu_sayfalama(tmp_path):
    products = [{"label": f"Urun {i}"} for i in range(20)]
    state = _state(tmp_path, {})
    _, rows = durum_gorunumu(products, state, sayfa=1)
    nav = [r for r in rows if any(b.get("text") == "▶️" for b in r)]
    assert nav and "2/3" in nav[0][1]["text"]
    kartlar = [r for r in rows if r[0].get("callback_data", "").startswith("kart|")]
    assert len(kartlar) == 8


def test_durum_gorunumu_sadece_sorunlu_bos(tmp_path):
    state = _state(tmp_path, {"A": {"last_good_ts": time.time(),
                                    "last_good_price": 5.0}})
    metin, _ = durum_gorunumu([{"label": "A"}], state, sadece_sorunlu=True)
    assert "Sorunlu ürün yok" in metin


def test_sparkline():
    from datetime import date, timedelta
    bugun = date.today()
    dmin = {(bugun - timedelta(days=i)).isoformat(): 100 - i for i in range(10)}
    cizgi = sparkline({"daily_min": dmin})
    assert len(cizgi) == 10
    assert cizgi[0] < cizgi[-1]          # artan fiyat → yükselen barlar
    assert sparkline({}) == ""           # veri yoksa boş


def test_kart_gorunumu_icerik():
    st = {"last_good_price": 105.0, "last_good_ts": time.time(),
          "last_good_host": "amazon.com.tr"}
    p = {"label": "Test Ürünü", "url": "https://www.amazon.com.tr/dp/X",
         "price_threshold_tl": 100}
    metin, rows = kart_gorunumu(p, st)
    assert "Test Ürünü" in metin and "🎯" in metin and "5₺ kaldı" in metin
    duz = [b for r in rows for b in r]
    datalar = [b.get("callback_data", "") for b in duz]
    kid = kisa_id("Test Ürünü")
    for beklenen in (f"hedefsec|{kid}", f"akk|{kid}", f"durdur|{kid}",
                     f"kaynak|{kid}", f"sil|{kid}", f"grf|{kid}"):
        assert beklenen in datalar
    assert any(b.get("url") for b in duz)          # 🛒 Ürüne git linki


def test_hedef_secim_gorunumu():
    p = {"label": "X", "price_threshold_tl": 100}
    metin, rows = hedef_secim_gorunumu(p, {"last_good_price": 200.0})
    assert "Mevcut hedef" in metin
    datalar = [b["callback_data"] for r in rows for b in r if "callback_data" in b]
    assert f"hpct|{kisa_id('X')}|3" in datalar and f"helle|{kisa_id('X')}" in datalar


def test_isim_ara():
    products = [{"label": "Kingston Beast 32GB DDR5"},
                {"label": "AMD Ryzen 7 7800X3D"},
                {"label": "Kingston A400 SSD"}]
    assert len(isim_ara(products, "kingston")) == 2
    assert isim_ara(products, "ryzen 7800")[0]["label"].startswith("AMD")
    assert isim_ara(products, "yok boyle") == []
    assert isim_ara(products, "") == []


# ===================== BUTON AKIŞLARI (sahte notifier/manager) =====================

class FakeNotifier:
    chat_id = "42"
    token = "t"

    def __init__(self):
        self.gonderilen = []      # (tur, metin, rows)

    async def tg(self, method, **kw):
        return {}

    async def send(self, text):
        self.gonderilen.append(("send", text, None))
        return True

    async def send_buttons(self, text, rows):
        self.gonderilen.append(("buttons", text, rows))
        return True

    async def edit_buttons(self, mid, text, rows):
        self.gonderilen.append(("edit", text, rows))
        return True


class FakeManager:
    context = sites = throttle = None

    def __init__(self):
        self.synclendi = []

    def sync(self, products, force=frozenset()):
        self.synclendi.append((len(products), set(force)))


@pytest.fixture
def ortam(tmp_path, monkeypatch):
    """Geçici products.yaml + overlay + state ile tam akış ortamı."""
    pyaml = tmp_path / "products.yaml"
    pyaml.write_text(yaml.safe_dump({"products": [
        {"label": "Urun A", "url": "http://a", "price_threshold_tl": 100},
        {"label": "Urun B", "url": "http://b"},
    ]}), encoding="utf-8")
    monkeypatch.setattr(konfig, "PRODUCTS_YAML", pyaml)
    monkeypatch.setattr(konfig, "TELEGRAM_URUNLER", tmp_path / "tg.yaml")
    state = veri.State(tmp_path / "state.json")
    shared = {"products": konfig.load_products()}
    return FakeNotifier(), FakeManager(), state, shared


def _cb(data):
    return {"id": "1", "data": data,
            "message": {"chat": {"id": 42}, "message_id": 7}}


def _isle(cb, n, s, st, m):
    asyncio.run(arayuz._callback_isle(cb, n, s, st, m, context=None))


def test_cb_durum_listesi(ortam):
    n, m, state, shared = ortam
    _isle(_cb("d|0"), n, shared, state, m)
    tur, metin, rows = n.gonderilen[-1]
    assert tur == "edit" and "2 ürün" in metin
    assert any(r[0]["callback_data"].startswith("kart|") for r in rows)


# ===================== GEZİNME (menü + bağlamsal geri) =====================

def test_ana_menu_gorunumu(tmp_path):
    from takipbotu.arayuz import ana_menu_gorunumu
    state = veri.State(tmp_path / "s.json")
    products = [{"label": "A"}, {"label": "B"}]
    metin, rows = ana_menu_gorunumu(products, state)
    datalar = [b["callback_data"] for r in rows for b in r if "callback_data" in b]
    assert "Ana Menü" in metin
    assert "d|0" in datalar and "setler" in datalar and "grftum" in datalar
    assert "yardim" in datalar


def test_urun_satiri_origin_kodlar():
    from takipbotu.arayuz import urun_satiri
    r = urun_satiri({"label": "X"}, {}, "s abc")  # boşluk olmaz ama format testi
    assert r[0]["callback_data"].startswith("kart|")
    r2 = urun_satiri({"label": "X"}, {}, "sABC123")
    assert r2[0]["callback_data"].endswith("|sABC123")   # set origin taşınır


def test_kart_baglamsal_geri():
    from takipbotu.arayuz import kart_gorunumu
    p = {"label": "X", "url": "http://a"}
    _, rows = kart_gorunumu(p, {"last_good_price": 100.0}, geri_cb="set|abc123")
    # son satır nav: [⬅️ Geri→set] [🏠 Menü]
    nav = rows[-1]
    assert nav[0]["callback_data"] == "set|abc123"
    assert nav[-1]["callback_data"] == "menu"


def test_kart_geri_sayfayi_korur():
    """3. sayfadan girip geri basınca yine 3. sayfaya dönmeli (hep 1'e değil)."""
    from takipbotu.arayuz import _kart_geri
    kid = "abc"
    assert _kart_geri({"kart_ori": {kid: "d3"}}, kid) == "d|3"
    assert _kart_geri({"kart_ori": {kid: "sor2"}}, kid) == "sor|2"
    assert _kart_geri({"kart_ori": {kid: "d"}}, kid) == "d|0"      # eski/varsayılan
    assert _kart_geri({"kart_ori": {kid: "sor"}}, kid) == "sor|0"
    assert _kart_geri({"kart_ori": {kid: "s1a2b3c4d5"}}, kid) == "set|1a2b3c4d5"
    assert _kart_geri({}, kid) == "d|0"                            # kayıt yoksa


def test_durum_urun_satiri_sayfa_origini_tasir(tmp_path):
    """3. sayfadaki ürün satırı callback'i 'd3' origin'i taşımalı."""
    products = [{"label": f"U{i}"} for i in range(20)]  # 3 sayfa
    state = veri.State(tmp_path / "s.json")
    _, rows = durum_gorunumu(products, state, sayfa=2)
    kart_cb = next(r[0]["callback_data"] for r in rows
                   if r[0].get("callback_data", "").startswith("kart|"))
    assert kart_cb.endswith("|d2")             # 3. sayfa (0-index 2) origin'i


def test_cb_menu_ve_yardim(ortam):
    n, m, state, shared = ortam
    _isle(_cb("menu"), n, shared, state, m)
    assert "Ana Menü" in n.gonderilen[-1][1]
    _isle(_cb("yardim"), n, shared, state, m)
    assert n.gonderilen[-1][0] == "edit"


def test_cb_kart_setten_gelince_geri_sete_doner(ortam):
    """Setten açılan kartın 'geri'si sete döner (kullanıcının asıl istediği)."""
    n, m, state, shared = ortam
    kid = kisa_id("Urun A")
    sid = kisa_id("PC")
    konfig.set_urun("PC", "Urun A")
    shared["products"] = konfig.load_products()
    # setten kart aç: kart|kid|s<sid>
    _isle(_cb(f"kart|{kid}|s{sid}"), n, shared, state, m)
    _, _, rows = n.gonderilen[-1]
    nav = rows[-1]
    assert nav[0]["callback_data"] == f"set|{sid}"       # sete döner
    # listeden aç: geri listeye
    _isle(_cb(f"kart|{kid}|d"), n, shared, state, m)
    assert n.gonderilen[-1][2][-1][0]["callback_data"] == "d|0"


def test_cb_kart_acilir(ortam):
    n, m, state, shared = ortam
    _isle(_cb(f"kart|{kisa_id('Urun A')}"), n, shared, state, m)
    tur, metin, _ = n.gonderilen[-1]
    assert tur == "edit" and "Urun A" in metin


def test_cb_sil_onay_ve_geri_al(ortam):
    n, m, state, shared = ortam
    kid = kisa_id("Urun A")
    _isle(_cb(f"sil|{kid}"), n, shared, state, m)
    assert "çıkarılsın mı" in n.gonderilen[-1][1]

    _isle(_cb(f"silon|{kid}"), n, shared, state, m)
    assert "İzlemeden çıkarıldı" in n.gonderilen[-1][1]
    assert all(p["label"] != "Urun A" for p in shared["products"])
    assert m.synclendi                       # izleyiciler eşitlendi

    _isle(_cb(f"geria|{kid}"), n, shared, state, m)
    assert "Geri alındı" in n.gonderilen[-1][1]
    assert any(p["label"] == "Urun A" for p in shared["products"])


def test_cb_duraklat_devam(ortam):
    n, m, state, shared = ortam
    kid = kisa_id("Urun B")
    _isle(_cb(f"durdur|{kid}"), n, shared, state, m)
    p = arayuz.urun_bul(shared["products"], kid)
    assert p.get("paused") is True
    assert "⏸" in n.gonderilen[-1][1]

    _isle(_cb(f"devam|{kid}"), n, shared, state, m)
    p = arayuz.urun_bul(shared["products"], kid)
    assert not p.get("paused")


def test_cb_hedef_yuzdeyle(ortam):
    n, m, state, shared = ortam
    kid = kisa_id("Urun A")
    state.get("Urun A")["last_good_price"] = 1000.0
    _isle(_cb(f"hpct|{kid}|10"), n, shared, state, m)
    p = arayuz.urun_bul(shared["products"], kid)
    assert p["price_threshold_tl"] == 900.0
    assert ("Urun A" in n.gonderilen[-1][1])     # kart güncellendi


def test_cb_sustur(ortam):
    n, m, state, shared = ortam
    kid = kisa_id("Urun A")
    _isle(_cb(f"sustur|{kid}"), n, shared, state, m)
    assert state.get("Urun A")["mute_until"] > time.time() + 6 * 24 * 3600
    # hedef değişince susturma kalkmalı
    state.get("Urun A")["last_good_price"] = 1000.0
    _isle(_cb(f"hpct|{kid}|5"), n, shared, state, m)
    assert "mute_until" not in state.get("Urun A")


def test_cb_bilinmeyen_urun(ortam):
    n, m, state, shared = ortam
    _isle(_cb("kart|0000000000"), n, shared, state, m)
    assert "listede yok" in n.gonderilen[-1][1]


def test_cb_yabanci_sohbet_yok_sayilir(ortam):
    n, m, state, shared = ortam
    cb = {"id": "1", "data": "d|0",
          "message": {"chat": {"id": 999}, "message_id": 7}}
    asyncio.run(arayuz._callback_isle(cb, n, shared, state, m, context=None))
    assert n.gonderilen == []
