# -*- coding: utf-8 -*-
"""Kalıcı veri katmanı: state.json (bildirim durumu + günlük minimumlar),
fiyat geçmişi (SQLite) ve etiket göçü.

Fiyat geçmişi veri.db'de tutulur (WAL modu — tek yazarlı, çökmeye dayanıklı).
Eski fiyat_gecmisi.csv ilk açılışta bir kez içeri aktarılır ve .eski uzantısıyla
arşivlenir; /csv komutu DB'den dışa aktarır. CSV'nin sınırsız büyüme ve tam
tarama sorunları böylece biter."""
import asyncio
import csv
import json
import logging
import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from . import konfig

HISTORY_CSV = konfig.HISTORY_CSV
VERI_DB = konfig.VERI_DB

_baglantilar: dict[Path, sqlite3.Connection] = {}


def _db() -> sqlite3.Connection:
    """veri.db bağlantısı (yol başına tekil). İlk açılışta şema kurulur ve
    varsa eski CSV bir kez içeri aktarılır."""
    yol = VERI_DB
    conn = _baglantilar.get(yol)
    if conn is not None:
        return conn
    conn = sqlite3.connect(yol)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS okumalar(
        id INTEGER PRIMARY KEY,
        ts TEXT NOT NULL,
        urun TEXT NOT NULL,
        site TEXT NOT NULL,
        fiyat REAL,
        stok INTEGER,
        kaynak TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_okumalar_urun_ts "
                 "ON okumalar(urun, ts)")
    conn.commit()
    _baglantilar[yol] = conn
    _csv_ice_aktar(conn)
    return conn


def _csv_ice_aktar(conn: sqlite3.Connection) -> None:
    """Tek seferlik göç: DB boşsa ve eski CSV varsa satırları içeri alır,
    CSV'yi .eski olarak arşivler (tek doğruluk kaynağı DB kalsın)."""
    if conn.execute("SELECT COUNT(*) FROM okumalar").fetchone()[0]:
        return
    if not HISTORY_CSV.exists():
        return
    aktarilan = 0
    with open(HISTORY_CSV, encoding="utf-8") as f:
        rd = csv.reader(f, delimiter=";")
        header = next(rd, None) or []
        v3 = "site" in header
        for r in rd:
            try:
                if v3:
                    ts, urun, site, fiyat, stok, kaynak = (r + [""] * 6)[:6]
                else:
                    ts, urun, fiyat, stok, kaynak = (r + [""] * 5)[:5]
                    site = "?"
                if not fiyat:
                    continue
                conn.execute(
                    "INSERT INTO okumalar(ts, urun, site, fiyat, stok, kaynak) "
                    "VALUES(?,?,?,?,?,?)",
                    (ts, urun, site, float(fiyat),
                     int(stok) if stok in ("0", "1") else None, kaynak))
                aktarilan += 1
            except (IndexError, ValueError):
                continue
    conn.commit()
    if aktarilan:
        arsiv = HISTORY_CSV.with_suffix(".csv.eski")
        try:
            os.replace(HISTORY_CSV, arsiv)
        except OSError:
            pass
        logging.info(f"Fiyat geçmişi SQLite'a taşındı: {aktarilan} kayıt "
                     f"(eski dosya: {arsiv.name})")


def gecmis_oku(urun: str | None = None) -> list[dict]:
    """Grafik için okuma listesi (fiyatı olanlar, zaman sıralı)."""
    q = ("SELECT ts, urun, site, fiyat FROM okumalar "
         "WHERE fiyat IS NOT NULL")
    args: tuple = ()
    if urun is not None:
        q += " AND urun = ?"
        args = (urun,)
    q += " ORDER BY ts"
    return [{"zaman": ts, "urun": u, "site": s, "fiyat": f}
            for ts, u, s, f in _db().execute(q, args).fetchall()]


def set_toplam_serisi(keys: list[str], gun: int = 60) -> list[tuple[str, float]]:
    """Set toplam grafiği için günlük seri: her gün, TÜM üyelerin o günkü
    minimum fiyatları toplamı. Bir üyenin verisi olmayan gün atlanır ki
    'ürün eklendi/okunamadı' günleri toplamda sahte sıçrama yapmasın."""
    if not keys:
        return []
    q = ("SELECT date(ts) g, urun, MIN(fiyat) FROM okumalar "
         f"WHERE fiyat IS NOT NULL AND urun IN ({','.join('?' * len(keys))}) "
         "AND ts >= date('now', ?) GROUP BY g, urun")
    gunluk: dict[str, dict[str, float]] = {}
    for g, urun, f in _db().execute(q, (*keys, f"-{gun} day")).fetchall():
        gunluk.setdefault(g, {})[urun] = f
    return [(g, sum(v.values())) for g, v in sorted(gunluk.items())
            if len(v) == len(keys)]


def fiyat_baglami(urun: str, guncel: float) -> dict | None:
    """'Bu iyi bir fiyat mı?' bağlamı (Keepa mantığı): son 90 günün günlük
    minimumlarından dip/medyan/yüzdelik + tüm zamanların dibi. En az 5 günlük
    veri yoksa None (yanıltıcı bağlam sunma)."""
    import statistics
    vals = [f for (f,) in _db().execute(
        "SELECT MIN(fiyat) FROM okumalar WHERE urun = ? AND fiyat IS NOT NULL "
        "AND ts >= date('now', '-90 day') GROUP BY date(ts)", (urun,)).fetchall()]
    if len(vals) < 5:
        return None
    dip90, medyan90 = min(vals), statistics.median(vals)
    # yüzdelik: 90 günün yüzde kaçında bugünkünden pahalıydı? (yüksek = ucuz gün)
    yuzde = round(sum(1 for v in vals if v >= guncel) / len(vals) * 100)
    satir = _db().execute(
        "SELECT fiyat, ts FROM okumalar WHERE urun = ? AND fiyat IS NOT NULL "
        "ORDER BY fiyat ASC, ts ASC LIMIT 1", (urun,)).fetchone()
    tum_dip, tum_dip_ts = (satir[0], satir[1][:10]) if satir else (None, "")
    if guncel <= dip90 * 1.02:
        sinyal = "🟢 dip bölgesi"
    elif guncel <= medyan90:
        sinyal = "🟡 ortalamanın altı"
    else:
        sinyal = "🔴 pahalı dönem"
    return {"dip90": dip90, "medyan90": medyan90, "yuzde": yuzde,
            "tum_dip": tum_dip, "tum_dip_tarih": tum_dip_ts,
            "gun_sayisi": len(vals), "sinyal": sinyal}


def baslat() -> None:
    """DB'yi açar (şema + gerekiyorsa CSV göçü). Bot açılışında çağrılır ki
    göç, ilk fiyat okumasını beklemeden yapılsın."""
    _db()


def csv_disari_aktar(hedef: Path) -> int:
    """/csv komutu: DB'deki tüm geçmişi CSV'ye yazar, satır sayısını döndürür."""
    rows = _db().execute(
        "SELECT ts, urun, site, fiyat, stok, kaynak FROM okumalar ORDER BY ts"
    ).fetchall()
    with open(hedef, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["zaman", "urun", "site", "fiyat", "stok", "kaynak"])
        for ts, urun, site, fiyat, stok, kaynak in rows:
            w.writerow([ts, urun, site,
                        f"{fiyat:.2f}" if fiyat is not None else "",
                        "" if stok is None else str(stok), kaynak])
    return len(rows)


class State:
    """state.json — mükerrer bildirim engelleme + son iyi fiyat + günlük minimumlar.
    Atomik yazılır (tmp + replace): bot yazma sırasında ölse bile dosya bozulmaz."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            self.data = {}

    def get(self, key: str) -> dict:
        return self.data.setdefault(key, {})

    async def save(self) -> None:
        async with self.lock:
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, self.path)


HISTORY_LOCK = asyncio.Lock()


async def append_history(label: str, site: str, price: float | None,
                         in_stock, source: str) -> None:
    from datetime import datetime
    async with HISTORY_LOCK:
        conn = _db()
        conn.execute(
            "INSERT INTO okumalar(ts, urun, site, fiyat, stok, kaynak) "
            "VALUES(?,?,?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), label, site, price,
             {True: 1, False: 0}.get(in_stock), source))
        conn.commit()


# --- Günlük minimum takibi: 30-gün-dibi sinyali + 7 günlük trend buradan beslenir ---

def gunluk_min_guncelle(st: dict, fp: float) -> None:
    """Bugünün en düşük okumasını state'e işler, 35 günden eskiyi budar."""
    dmin = st.setdefault("daily_min", {})
    bugun = date.today().isoformat()
    dmin[bugun] = min(fp, dmin.get(bugun, fp))
    sinir = (date.today() - timedelta(days=35)).isoformat()
    for g in [g for g in dmin if g < sinir]:
        del dmin[g]


def dip30_oncesi(st: dict) -> tuple[float | None, int]:
    """Bugün HARİÇ son 30-35 günün en düşük fiyatı + kaç günlük veri olduğu."""
    dmin = st.get("daily_min") or {}
    bugun = date.today().isoformat()
    onceki = [v for g, v in dmin.items() if g != bugun]
    if not onceki:
        return None, 0
    return min(onceki), len(onceki)


def yedi_gun_degisim(st: dict) -> float | None:
    """Son iyi fiyatın ~7 gün önceki günlük minimuma göre % değişimi."""
    fp = st.get("last_good_price")
    dmin = st.get("daily_min") or {}
    if not fp or not dmin:
        return None
    bugun = date.today()
    adaylar = []
    for g, v in dmin.items():
        try:
            yas = (bugun - date.fromisoformat(g)).days
        except ValueError:
            continue
        if 4 <= yas <= 10:
            adaylar.append((abs(yas - 7), v))
    if not adaylar:
        return None
    eski = min(adaylar)[1]
    if eski <= 0:
        return None
    return (fp - eski) / eski * 100


# ===================== ETİKET GÖÇÜ =====================

def gecmis_etiket_degistir(eski: str, yeni: str) -> None:
    """veri.db'deki ürün adını günceller (grafik geçmişi kopmasın)."""
    conn = _db()
    conn.execute("UPDATE okumalar SET urun = ? WHERE urun = ?", (yeni, eski))
    conn.commit()


def etiket_gocu(state: State, products: list[dict]) -> bool:
    """Etiket değişse de geçmiş kaybolmasın. state/geçmiş/hedefler etikete göre
    anahtarlıdır (çoklu kaynakta URL ürünü temsil etmez); ürünü yeniden
    adlandırmak cooldown'u, günlük minimumları ve grafik geçmişini sıfırlıyordu.
    Eşleştirme URL parmak iziyle: izleyici her turda ürünün URL'lerini state'e
    yazar; sahipsiz kalan eski kayıt, URL'leri kesişen ve kendi kaydı olmayan
    yeni ürüne aktarılır. True dönerse ürün listesi yeniden yüklenmelidir
    (taşınan hedef/ek-kaynak overlay'i uygulansın diye)."""
    mevcut = {konfig.product_key(p) for p in products}
    tasindi = False
    for eski in [k for k in state.data if not k.startswith("_") and k not in mevcut]:
        eski_urls = set(state.data[eski].get("urls") or [])
        if not eski_urls:
            continue
        for p in products:
            yeni = konfig.product_key(p)
            if yeni in state.data or not eski_urls & set(konfig.product_urls(p)):
                continue
            state.data[yeni] = state.data.pop(eski)
            gecmis_etiket_degistir(eski, yeni)
            d = konfig._tg_dosya()
            degisti = False
            for alan in ("hedefler", "ek_kaynaklar"):
                if eski in d[alan]:
                    d[alan][yeni] = d[alan].pop(eski)
                    degisti = True
            if degisti:
                konfig.save_yaml_atomic(konfig.TELEGRAM_URUNLER, d)
            logging.info(f"Etiket değişikliği algılandı: '{eski}' → '{yeni}' — "
                         "geçmiş taşındı (state + veri.db + hedef/ek-kaynak).")
            tasindi = True
            break
    return tasindi
