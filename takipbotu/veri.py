# -*- coding: utf-8 -*-
"""Kalıcı veri katmanı: state.json (bildirim durumu + günlük minimumlar),
fiyat geçmişi ve etiket göçü."""
import asyncio
import csv
import json
import logging
import os
from datetime import date, timedelta
from pathlib import Path

from . import konfig

HISTORY_CSV = konfig.HISTORY_CSV


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
        yeni = not HISTORY_CSV.exists()
        with open(HISTORY_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter=";")
            if yeni:
                w.writerow(["zaman", "urun", "site", "fiyat", "stok", "kaynak"])
            w.writerow([
                datetime.now().isoformat(timespec="seconds"),
                label,
                site,
                f"{price:.2f}" if price is not None else "",
                {True: "1", False: "0"}.get(in_stock, ""),
                source,
            ])


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

def csv_etiket_degistir(eski: str, yeni: str) -> None:
    """fiyat_gecmisi.csv'deki ürün adını günceller (atomik: tmp + replace)."""
    if not HISTORY_CSV.exists():
        return
    with open(HISTORY_CSV, encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f, delimiter=";"))
    for r in rows:
        if len(r) >= 2 and r[1] == eski:
            r[1] = yeni
    tmp = HISTORY_CSV.with_suffix(".csv.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        csv.writer(f, delimiter=";").writerows(rows)
    os.replace(tmp, HISTORY_CSV)


def etiket_gocu(state: State, products: list[dict]) -> bool:
    """Etiket değişse de geçmiş kaybolmasın. state/CSV/hedefler etikete göre
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
            csv_etiket_degistir(eski, yeni)
            d = konfig._tg_dosya()
            degisti = False
            for alan in ("hedefler", "ek_kaynaklar"):
                if eski in d[alan]:
                    d[alan][yeni] = d[alan].pop(eski)
                    degisti = True
            if degisti:
                konfig.save_yaml_atomic(konfig.TELEGRAM_URUNLER, d)
            logging.info(f"Etiket değişikliği algılandı: '{eski}' → '{yeni}' — "
                         "geçmiş taşındı (state + CSV + hedef/ek-kaynak).")
            tasindi = True
            break
    return tasindi
