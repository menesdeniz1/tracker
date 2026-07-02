# -*- coding: utf-8 -*-
"""TL fiyat parser'ı — botun en riskli fonksiyonu. Türkçe/İngilizce binlik ve
ondalık ayraç kombinasyonlarının tamamı burada sabitlenir; bir refactor bu
kuralları bozarsa CI anında yakalar."""
import pytest

from takip_botu_pro import parse_try_amount, tl, pct


@pytest.mark.parametrize("girdi, beklenen", [
    # Türkçe biçimler
    ("53.599 TL", 53599.0),        # nokta = binlik
    ("53.599,50 TL", 53599.5),     # nokta binlik + virgül ondalık
    ("1.053.599 TL", 1053599.0),   # çift nokta = binlik
    ("599,5 TL", 599.5),           # virgül ondalık (tek hane)
    ("53599,50", 53599.5),
    ("₺2.499,00", 2499.0),
    # İngilizce yerelli siteler
    ("53,599.50", 53599.5),
    ("1,053,599", 1053599.0),
    ("53,599", 53599.0),           # virgül + tam 3 hane = binlik
    # Kenar durumlar
    ("12.345", 12345.0),           # nokta + tam 3 hane = binlik
    ("12.34", 12.34),              # nokta + 2 hane = ondalık
    ("1.2", 1.2),
    (12345, 12345.0),              # sayı tipi doğrudan
    (99.9, 99.9),
])
def test_gecerli_fiyatlar(girdi, beklenen):
    assert parse_try_amount(girdi) == pytest.approx(beklenen)


@pytest.mark.parametrize("girdi", [
    None, "", "abc", "TL", 0, -5, 100_000_000, "0", ",", ".",
])
def test_gecersiz_fiyatlar(girdi):
    assert parse_try_amount(girdi) is None


def test_tl_bicimleme():
    assert tl(None) == "—"
    assert tl(1234.5) == "1.234,50 TL"
    assert tl(53599.0) == "53.599,00 TL"


def test_pct_bicimleme():
    assert pct(-4.23) == "↓%4,2"
    assert pct(2.1) == "↑%2,1"
