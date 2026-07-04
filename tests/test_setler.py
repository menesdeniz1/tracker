# -*- coding: utf-8 -*-
"""Setler + fiyat bağlamı + değişenler raporu + sessiz saat + acil hedef +
ölü kaynak eleme — 'sırayla yap' paketinin testleri."""
import asyncio
import time
from datetime import date, datetime, timedelta

import yaml

from takipbotu import arayuz, izleyici, konfig, veri
from takipbotu.karar import en_iyi_kaynak


def _ortam(tmp_path, monkeypatch, urunler=None):
    pyaml = tmp_path / "products.yaml"
    pyaml.write_text(yaml.safe_dump({"products": urunler or [
        {"label": "CPU", "url": "http://a", "price_threshold_tl": 100},
        {"label": "GPU", "url": "http://b", "price_threshold_tl": 200},
    ]}), encoding="utf-8")
    monkeypatch.setattr(konfig, "PRODUCTS_YAML", pyaml)
    monkeypatch.setattr(konfig, "TELEGRAM_URUNLER", tmp_path / "tg.yaml")
    monkeypatch.setattr(veri, "VERI_DB", tmp_path / "veri.db")
    monkeypatch.setattr(veri, "HISTORY_CSV", tmp_path / "yok.csv")
    state = veri.State(tmp_path / "state.json")
    return state


# ---------- konfig: set yönetimi ----------

def test_set_kur_cikar_bosalinca_silinir(tmp_path, monkeypatch):
    _ortam(tmp_path, monkeypatch)
    konfig.set_urun("PC", "CPU")
    konfig.set_urun("PC", "GPU")
    assert konfig.setleri_getir()["PC"]["urunler"] == ["CPU", "GPU"]
    konfig.set_hedef("PC", 90000.0)
    assert konfig.setleri_getir()["PC"]["hedef"] == 90000.0
    konfig.set_urun("PC", "CPU", ekle=False)
    konfig.set_urun("PC", "GPU", ekle=False)
    assert "PC" not in konfig.setleri_getir()      # boşalan set silinir


def test_urun_silinince_setten_duser(tmp_path, monkeypatch):
    _ortam(tmp_path, monkeypatch)
    konfig.set_urun("PC", "CPU")
    konfig.tg_urun_sil("CPU")
    assert "PC" not in konfig.setleri_getir()


def test_acil_hedef_yukleme(tmp_path, monkeypatch):
    _ortam(tmp_path, monkeypatch)
    konfig.tg_acil_hedef("CPU", 80.0)
    p = next(q for q in konfig.load_products() if q["label"] == "CPU")
    assert p["price_threshold2_tl"] == 80.0
    konfig.tg_acil_hedef("CPU", None)
    p = next(q for q in konfig.load_products() if q["label"] == "CPU")
    assert "price_threshold2_tl" not in p


# ---------- veri: set serisi + fiyat bağlamı ----------

def test_set_toplam_serisi_ortak_gun(tmp_path, monkeypatch):
    _ortam(tmp_path, monkeypatch)
    asyncio.run(veri.append_history("CPU", "s", 100.0, None, "seçici"))
    asyncio.run(veri.append_history("GPU", "s", 200.0, None, "seçici"))
    seri = veri.set_toplam_serisi(["CPU", "GPU"])
    assert len(seri) == 1 and seri[0][1] == 300.0
    # bir üyenin HİÇ verisi yoksa grafik çıkmaz
    assert veri.set_toplam_serisi(["CPU", "Hayalet"]) == []


def test_set_toplam_serisi_gec_katilan_uye_geri_doldurulur(tmp_path, monkeypatch):
    """Sonradan eklenen üye, katılmadan önceki günlerde ilk fiyatıyla sayılır —
    ürün eklemek set grafik geçmişini silmez, çizgi sürekli olur."""
    _ortam(tmp_path, monkeypatch)
    conn = veri._db()
    # CPU iki gün, GPU sadece bugün (geç katıldı)
    dun = (date.today() - timedelta(days=1)).isoformat()
    conn.execute("INSERT INTO okumalar(ts,urun,site,fiyat,stok,kaynak) "
                 "VALUES(?,?,?,?,NULL,'s')", (f"{dun}T10:00:00", "CPU", "s", 100.0))
    conn.commit()
    asyncio.run(veri.append_history("CPU", "s", 110.0, None, "s"))
    asyncio.run(veri.append_history("GPU", "s", 200.0, None, "s"))
    seri = veri.set_toplam_serisi(["CPU", "GPU"])
    assert len(seri) == 2                      # dün + bugün (tek noktaya düşmez)
    assert seri[0] == (dun, 300.0)             # dün: 100 + GPU'nun ilk fiyatı 200
    assert seri[1][1] == 310.0                 # bugün: 110 + 200


def test_fiyat_baglami_ve_sinyal(tmp_path, monkeypatch):
    _ortam(tmp_path, monkeypatch)
    conn = veri._db()
    for i in range(10):                       # 10 günlük veri: 100..109
        g = (date.today() - timedelta(days=i)).isoformat()
        conn.execute("INSERT INTO okumalar(ts,urun,site,fiyat,stok,kaynak) "
                     "VALUES(?,?,?,?,NULL,'seçici')",
                     (f"{g}T10:00:00", "CPU", "s", 100.0 + i))
    conn.commit()
    b = veri.fiyat_baglami("CPU", 100.0)
    assert b["dip90"] == 100.0 and b["tum_dip"] == 100.0
    assert b["yuzde"] == 100 and b["sinyal"].startswith("🟢")
    assert veri.fiyat_baglami("CPU", 120.0)["sinyal"].startswith("🔴")
    assert veri.fiyat_baglami("Hayalet", 50.0) is None     # veri yok → bağlam yok


# ---------- izleyici: toplamlar, rapor, sessiz saat, set bildirimi ----------

def test_set_toplamlari_ve_eksik(tmp_path, monkeypatch):
    state = _ortam(tmp_path, monkeypatch)
    konfig.set_urun("PC", "CPU")
    konfig.set_urun("PC", "GPU")
    state.get("CPU")["last_good_price"] = 100.0
    s = izleyici.set_toplamlari(state)[0]
    assert s["toplam"] == 100.0 and s["eksik"] == ["GPU"]


def test_degisim_raporu(tmp_path, monkeypatch):
    state = _ortam(tmp_path, monkeypatch)
    dun = (date.today() - timedelta(days=1)).isoformat()
    simdi = time.time()
    state.data = {
        "CPU": {"last_good_price": 95.0, "last_good_ts": simdi,
                "daily_min": {dun: 100.0}},
        "GPU": {"last_good_price": 210.0, "last_good_ts": simdi,
                "daily_min": {dun: 200.0}},
    }
    products = [{"label": "CPU"}, {"label": "GPU"}, {"label": "Sessiz"}]
    r = izleyici.degisim_raporu(products, state)
    assert "Düşenler" in r and "CPU" in r and "↓%5,0" in r
    assert "Yükselenler" in r and "GPU" in r
    assert "1 okunamıyor" in r                 # 'Sessiz' hiç okunmamış
    # hareket yoksa tek satır
    state.data = {"CPU": {"last_good_price": 100.0, "last_good_ts": simdi,
                          "daily_min": {dun: 100.0}}}
    assert "kayda değer" in izleyici.degisim_raporu([{"label": "CPU"}], state)


def test_sessiz_saat(monkeypatch):
    class Sabit(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 1, 3, 0)
    monkeypatch.setattr(izleyici, "datetime", Sabit)
    assert izleyici.sessiz_saat_mi({"quiet_hours": "0-8"})
    assert izleyici.sessiz_saat_mi({"quiet_hours": "23-7"})    # gece yarısı aşan
    assert not izleyici.sessiz_saat_mi({"quiet_hours": "9-18"})
    assert not izleyici.sessiz_saat_mi({})
    assert not izleyici.sessiz_saat_mi({"quiet_hours": "bozuk"})


class _Notifier:
    def __init__(self):
        self.mesajlar = []

    async def send(self, text):
        self.mesajlar.append(text)
        return True


def test_set_toplam_bildirimi(tmp_path, monkeypatch):
    state = _ortam(tmp_path, monkeypatch)
    konfig.set_urun("PC", "CPU")
    konfig.set_urun("PC", "GPU")
    konfig.set_hedef("PC", 250.0)
    state.get("CPU")["last_good_price"] = 100.0
    state.get("GPU")["last_good_price"] = 140.0
    n = _Notifier()
    asyncio.run(izleyici.set_toplam_kontrol(n, state, "CPU"))
    assert n.mesajlar and "SET HEDEFTE" in n.mesajlar[0]
    # 24 saat cooldown: ikinci çağrı bildirmez
    asyncio.run(izleyici.set_toplam_kontrol(n, state, "CPU"))
    assert len(n.mesajlar) == 1
    # eksik üye varsa bildirmez
    state.get("GPU").pop("last_good_price")
    state.get("_set:PC")["last_notify_ts"] = 0
    asyncio.run(izleyici.set_toplam_kontrol(n, state, "CPU"))
    assert len(n.mesajlar) == 1


def test_tg_ayri_yoklama_baglantisi():
    """getUpdates verilen ayrı bağlantıyı kullanmalı; diğer çağrılar ana
    bağlantıyı. (Yoklamanın buton/edit istekleriyle çakışmasını önler.)"""
    from takipbotu.bildirim import Notifier

    class FakeCtx:
        def __init__(self):
            self.cagrildi = False

        async def post(self, url, data=None, timeout=None):
            self.cagrildi = True

            class R:
                async def json(self_inner):
                    return {"ok": True, "result": "x"}
            return R()

    ana, poll = FakeCtx(), FakeCtx()
    n = Notifier(ana, {"telegram_bot_token": "t"})
    asyncio.run(n.tg("getUpdates", rq=poll))
    assert poll.cagrildi and not ana.cagrildi        # yoklama ayrı bağlantıda
    asyncio.run(n.tg("sendMessage"))
    assert ana.cagrildi                              # gönderim ana bağlantıda


# ---------- karar: ölü kaynak elenir ----------

def test_olu_kaynak_elenir():
    s1 = {"price": 50.0, "blocked": False, "dead": True,
          "variant_ok": True, "in_stock": None}
    s2 = {"price": 90.0, "blocked": False, "dead": False,
          "variant_ok": True, "in_stock": None}
    assert en_iyi_kaynak({"mode": "price"}, [s1, s2])["price"] == 90.0


# ---------- arayuz: set görünümleri ----------

def test_set_gorunumleri(tmp_path, monkeypatch):
    state = _ortam(tmp_path, monkeypatch)
    konfig.set_urun("PC", "CPU")
    konfig.set_urun("PC", "GPU")
    konfig.set_hedef("PC", 250.0)
    state.get("CPU")["last_good_price"] = 100.0
    state.get("GPU").update({"last_good_price": 140.0,
                             "last_good_ts": time.time()})
    state.get("CPU")["last_good_ts"] = time.time()

    metin, rows = arayuz.setler_gorunumu(state)
    assert "PC" in rows[0][0]["text"] and "240" in rows[0][0]["text"]

    products = konfig.load_products()
    metin, rows = arayuz.set_gorunumu("PC", products, state)
    assert "TOPLAM: 240,00 TL" in metin
    datalar = [b.get("callback_data", "") for r in rows for b in r]
    sid = arayuz.kisa_id("PC")
    assert f"sethedef|{sid}" in datalar and f"setgrf|{sid}" in datalar
    # üyeler pahalıdan ucuza sıralı: ilk ürün satırı GPU (140) olmalı
    urun_satirlari = [r for r in rows
                      if r[0].get("callback_data", "").startswith("kart|")]
    assert urun_satirlari[0][0]["text"].startswith("GPU")
    # alt bar dolu + HEDEFTE (240<=250)
    assert "██████████" in metin and "HEDEFTE" in metin.split("\n")[-1]

    p = products[0]
    metin, rows = arayuz.set_secim_gorunumu(p)
    datalar = [b["callback_data"] for r in rows for b in r]
    kid = arayuz.kisa_id("CPU")
    assert f"setcik|{kid}|{sid}" in datalar      # üye → çıkarma önerilir
    assert f"setyeni|{kid}" in datalar


def test_kart_set_uyeligi_gosterir(tmp_path, monkeypatch):
    _ortam(tmp_path, monkeypatch)
    konfig.set_urun("PC", "CPU")
    metin, _ = arayuz.kart_gorunumu(
        {"label": "CPU", "url": "http://a"}, {"last_good_price": 100.0})
    assert "📦 Set: PC" in metin


def test_yedek_al_ve_saglik(tmp_path, monkeypatch):
    """Yedek zip'i üç veri dosyasını içerir + son yedek zamanı işlenir;
    sağlık özeti temel bilgileri döker."""
    import zipfile

    state = _ortam(tmp_path, monkeypatch)
    # yedek_al konfig yollarını okur → hepsini tmp_path'e hizala
    monkeypatch.setattr(konfig, "BASE_DIR", tmp_path)
    monkeypatch.setattr(konfig, "VERI_DB", tmp_path / "veri.db")
    monkeypatch.setattr(konfig, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(konfig, "USER_DATA_DIR", str(tmp_path / "profil"))
    state.path = tmp_path / "state.json"

    asyncio.run(veri.append_history("CPU", "s", 100.0, None, "seçici"))  # veri.db
    asyncio.run(state.save())                                            # state.json
    konfig.set_urun("PC", "CPU")                                         # tg.yaml

    n = _Notifier()
    gonderilen = {}

    async def sahte_belge(path, caption=""):
        gonderilen["path"] = path
        return True
    n.send_document = sahte_belge

    assert asyncio.run(izleyici.yedek_al(n, state)) is True
    with zipfile.ZipFile(gonderilen["path"]) as z:
        adlar = z.namelist()
    assert {"veri.db", "state.json", "tg.yaml"} <= set(adlar)
    assert state.get("_meta")["last_backup_ts"] > 0

    ozet = izleyici.saglik_ozeti(konfig.load_products(), state)
    assert "Sağlık" in ozet and "Ayakta" in ozet and "Son yedek" in ozet


def test_kart_pazar_bilgisi(tmp_path, monkeypatch):
    _ortam(tmp_path, monkeypatch)
    st = {"last_good_price": 100.0,
          "pazar": {"satici_sayisi": 5, "ikinci_fiyat": 130.0}}
    metin, _ = arayuz.kart_gorunumu({"label": "CPU", "url": "http://a"}, st)
    assert "🏪 5 satıcı" in metin and "2. en ucuz: 130₺" in metin
    assert "belirgin ucuz" in metin            # 130 > 100*1.2 → tek satıcı uyarısı
    st["pazar"] = {"satici_sayisi": 1, "ikinci_fiyat": None}
    metin, _ = arayuz.kart_gorunumu({"label": "CPU", "url": "http://a"}, st)
    assert "⚠️ tek satıcı" in metin
