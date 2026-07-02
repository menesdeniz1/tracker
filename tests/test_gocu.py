# -*- coding: utf-8 -*-
"""Etiket değişikliğinde geçmiş göçü: state + veri.db + Telegram overlay URL
parmak iziyle yeni etikete taşınmalı; ilgisiz kayıtlara dokunulmamalı."""
import asyncio

from takipbotu import konfig, veri


def _kur(tmp_path, monkeypatch):
    """Geçici dosyalarla göç ortamı hazırlar."""
    monkeypatch.setattr(veri, "VERI_DB", tmp_path / "veri.db")
    monkeypatch.setattr(veri, "HISTORY_CSV", tmp_path / "gecmis.csv")
    monkeypatch.setattr(konfig, "TELEGRAM_URUNLER", tmp_path / "tg.yaml")

    asyncio.run(veri.append_history("Eski Ad", "amazon.com.tr", 100.0, None, "seçici"))
    asyncio.run(veri.append_history("Baska Urun", "n11.com", 50.0, None, "json-ld"))

    konfig.save_yaml_atomic(konfig.TELEGRAM_URUNLER, {
        "eklenen": [], "kaldirilan": [],
        "hedefler": {"Eski Ad": 90.0},
        "ek_kaynaklar": {"Eski Ad": ["http://akakce/x"]},
    })

    state = veri.State(tmp_path / "state.json")
    state.data = {
        "_meta": {"last_start_ts": 1},
        "Eski Ad": {"last_good_price": 100.0,
                    "urls": ["http://akakce/x", "http://magaza/a"]},
        "Baska Urun": {"last_good_price": 50.0, "urls": ["http://n11/b"]},
        "URLsuz Sahipsiz": {"last_good_price": 7.0},
    }
    products = [
        {"label": "Yeni Ad", "url": "http://magaza/a"},     # Eski Ad'ın yeni hali
        {"label": "Baska Urun", "url": "http://n11/b"},
        {"label": "Yepyeni Urun", "url": "http://baska/c"},
    ]
    return state, products


def test_goc_tasima(tmp_path, monkeypatch):
    state, products = _kur(tmp_path, monkeypatch)

    assert veri.etiket_gocu(state, products) is True

    # state yeni ada taşındı, içerik korundu
    assert "Yeni Ad" in state.data and "Eski Ad" not in state.data
    assert state.data["Yeni Ad"]["last_good_price"] == 100.0
    # ilgisiz kayıtlar yerinde
    assert state.data["Baska Urun"]["last_good_price"] == 50.0
    assert "URLsuz Sahipsiz" in state.data
    assert "Yepyeni Urun" not in state.data

    # veri.db'deki geçmiş yeni ada taşındı, diğer ürün korundu
    assert veri.gecmis_oku("Yeni Ad") and not veri.gecmis_oku("Eski Ad")
    assert veri.gecmis_oku("Baska Urun")

    # Telegram overlay anahtarları taşındı
    tg = konfig.load_yaml(konfig.TELEGRAM_URUNLER)
    assert tg["hedefler"] == {"Yeni Ad": 90.0}
    assert tg["ek_kaynaklar"] == {"Yeni Ad": ["http://akakce/x"]}


def test_goc_ikinci_calistirma_noop(tmp_path, monkeypatch):
    state, products = _kur(tmp_path, monkeypatch)
    veri.etiket_gocu(state, products)
    assert veri.etiket_gocu(state, products) is False


def test_goc_url_kesismiyorsa_dokunmaz(tmp_path, monkeypatch):
    state, _ = _kur(tmp_path, monkeypatch)
    products = [{"label": "Alakasiz", "url": "http://bambaska/z"}]
    assert veri.etiket_gocu(state, products) is False
    assert "Eski Ad" in state.data
