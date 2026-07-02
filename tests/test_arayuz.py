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


def test_urun_satiri_durumlari():
    assert urun_satiri({"label": "X", "paused": True}, {}).startswith("⏸")
    assert urun_satiri({"label": "X"}, {}).startswith("⚠️")
    st = {"last_good_price": 90.0, "last_good_ts": time.time()}
    assert "HEDEFTE" in urun_satiri({"label": "X", "price_threshold_tl": 100}, st)
    st2 = {"last_good_price": 110.0, "last_good_ts": time.time()}
    assert "%10" in urun_satiri({"label": "X", "price_threshold_tl": 100}, st2)


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
    urun_butonlari = [r[0]["text"] for r in rows if r[0].get("callback_data", "").startswith("kart|")]
    assert urun_butonlari[0].startswith("⚠️")
    assert urun_butonlari[-1].startswith("⏸")


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
    assert "Test Ürünü" in metin and "🎯" in metin and "%5,0 kaldı" in metin.replace(".", ",")
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
