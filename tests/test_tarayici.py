# -*- coding: utf-8 -*-
"""Host kuyruğu: geri çekilmedeki hosta kontrol ERTELENMELİ — semafor/kilit
dakikalarca işgal edilirse tüm bot donuyordu (üretim loglarındaki 15-57 dk
sessizliklerin kökü)."""
import asyncio

from takipbotu.tarayici import MAX_KUYRUK_BEKLEME, HostThrottle, Sites, check_once


def test_tahmini_bekleme_ve_ceza():
    t = HostThrottle(10)
    assert t.tahmini_bekleme("x.com") == 0.0
    t.penalize("x.com")                       # 300 sn ceza
    assert t.tahmini_bekleme("x.com") > MAX_KUYRUK_BEKLEME
    t.reward("x.com")
    assert t.backoff["x.com"] == 0.0          # ceza sıfırlanır (next_ok süresi dolar)


def test_cezali_hostta_kontrol_ertelenir():
    """Ceza MAX_KUYRUK_BEKLEME'yi aşıyorsa check_once tarayıcıya hiç dokunmadan
    'blocked' döner — context=None ile çağrılabilmesi bunun kanıtı."""
    t = HostThrottle(10)
    t.penalize("www.tebilon.com")             # 300 sn > 180 sn eşiği
    s = asyncio.run(check_once(
        None, Sites({}), t, {"label": "test", "mode": "price"},
        "https://www.tebilon.com/urun/"))
    assert s["blocked"] is True
    assert s["price"] is None


def test_kisa_bekleme_ertelenmez_kuyruga_girer():
    """Eşik altındaki bekleme normal akışa girer (context'e dokunur) —
    None context'te AttributeError bunun kanıtı."""
    t = HostThrottle(10)
    try:
        asyncio.run(check_once(None, Sites({}), t,
                               {"label": "test", "mode": "price"},
                               "https://www.ornek.com/urun/"))
        raise AssertionError("context'e dokunmadan döndü — kuyruğa girmedi!")
    except AttributeError:
        pass  # beklenen: None.new_page() — yani erteleme yapılmadı
