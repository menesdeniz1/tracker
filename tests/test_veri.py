# -*- coding: utf-8 -*-
"""SQLite veri katmanı: yazma/okuma, tek seferlik CSV göçü, etiket yeniden
adlandırma, CSV dışa aktarma."""
import asyncio
import csv

from takipbotu import veri


def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(veri, "VERI_DB", tmp_path / "veri.db")
    monkeypatch.setattr(veri, "HISTORY_CSV", tmp_path / "gecmis.csv")


def test_append_ve_oku(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    asyncio.run(veri.append_history("Urun A", "amazon.com.tr", 100.5, True, "seçici"))
    asyncio.run(veri.append_history("Urun A", "n11.com", None, None, "yok"))
    asyncio.run(veri.append_history("Urun B", "n11.com", 50.0, False, "json-ld"))

    rows = veri.gecmis_oku()
    assert len(rows) == 2                      # fiyatsız okuma grafiğe girmez
    assert rows[0]["urun"] == "Urun A" and rows[0]["fiyat"] == 100.5

    sadece_a = veri.gecmis_oku("Urun A")
    assert len(sadece_a) == 1 and sadece_a[0]["site"] == "amazon.com.tr"


def test_csv_goc_bir_kez_ve_arsivleme(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    with open(veri.HISTORY_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["zaman", "urun", "site", "fiyat", "stok", "kaynak"])
        w.writerow(["2026-07-01T10:00:00", "Eski Urun", "amazon.com.tr",
                    "99.90", "1", "seçici"])
        w.writerow(["2026-07-01T11:00:00", "Eski Urun", "n11.com", "", "", "yok"])
        w.writerow(["bozuk"])

    rows = veri.gecmis_oku()
    assert len(rows) == 1 and rows[0]["fiyat"] == 99.9
    # CSV arşivlendi, ikinci açılışta tekrar göç edilmez
    assert not veri.HISTORY_CSV.exists()
    assert (tmp_path / "gecmis.csv.eski").exists()
    assert len(veri.gecmis_oku()) == 1


def test_etiket_degistir(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    asyncio.run(veri.append_history("Eski Ad", "n11.com", 10.0, None, "seçici"))
    veri.gecmis_etiket_degistir("Eski Ad", "Yeni Ad")
    assert veri.gecmis_oku("Yeni Ad") and not veri.gecmis_oku("Eski Ad")


def test_csv_disari_aktar(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    asyncio.run(veri.append_history("U", "site", 12.34, True, "meta"))
    hedef = tmp_path / "export.csv"
    assert veri.csv_disari_aktar(hedef) == 1
    rows = list(csv.reader(open(hedef, encoding="utf-8"), delimiter=";"))
    assert rows[0] == ["zaman", "urun", "site", "fiyat", "stok", "kaynak"]
    assert rows[1][1:] == ["U", "site", "12.34", "1", "meta"]
