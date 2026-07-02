# -*- coding: utf-8 -*-
"""Grafik modülü: CSV okuma (v2/v3 düzenleri + bozuk satır toleransı) ve
hedef fiyat birleşimi (products.yaml + telegram_urunler.yaml overlay)."""
import grafik
import yaml


def test_read_history_v3_ve_bozuk_satirlar(tmp_path):
    p = tmp_path / "gecmis.csv"
    p.write_text(
        "zaman;urun;site;fiyat;stok;kaynak\n"
        "2026-07-01T10:00:00;Urun A;amazon.com.tr;100.50;1;seçici\n"
        "2026-07-01T11:00:00;Urun A;n11.com;;;yok\n"          # fiyatsız → atla
        "bozuk satır\n"                                        # kırık → atla
        "2026-07-01T12:00:00;Urun B;n11.com;50.00;;json-ld\n",
        encoding="utf-8")
    rows = grafik._read_history(p)
    assert len(rows) == 2
    assert rows[0]["urun"] == "Urun A" and rows[0]["site"] == "amazon.com.tr"
    assert rows[0]["fiyat"] == 100.5


def test_read_history_v2_eski_duzen(tmp_path):
    p = tmp_path / "gecmis.csv"
    p.write_text(
        "zaman;urun;fiyat;stok;kaynak\n"
        "2026-07-01T10:00:00;Urun A;100.50;1;seçici\n",
        encoding="utf-8")
    rows = grafik._read_history(p)
    assert len(rows) == 1 and rows[0]["site"] == "?"


def test_thresholds_overlay_birlesimi(tmp_path):
    products = tmp_path / "products.yaml"
    products.write_text(yaml.safe_dump({"products": [
        {"label": "A", "price_threshold_tl": 100},
        {"label": "B", "price_threshold_tl": 200},
        {"label": "Hedefsiz"},
    ]}), encoding="utf-8")
    (tmp_path / "telegram_urunler.yaml").write_text(yaml.safe_dump({
        "eklenen": [{"label": "TG Urunu", "price_threshold_tl": 300}],
        "hedefler": {"B": 150},          # /hedef ile değişmiş
    }), encoding="utf-8")

    h = grafik._thresholds(products)
    assert h["A"] == 100.0
    assert h["B"] == 150.0               # overlay products.yaml'ı ezer
    assert h["TG Urunu"] == 300.0
    assert "Hedefsiz" not in h


def test_thresholds_overlay_dosyasi_yoksa(tmp_path):
    products = tmp_path / "products.yaml"
    products.write_text(yaml.safe_dump({"products": [
        {"label": "A", "price_threshold_tl": 100}]}), encoding="utf-8")
    assert grafik._thresholds(products) == {"A": 100.0}
