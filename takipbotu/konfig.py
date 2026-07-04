# -*- coding: utf-8 -*-
"""Konfigürasyon: yollar, YAML okuma/yazma, ürün listesi birleşimi
(products.yaml + telegram_urunler.yaml overlay), etiket yardımcıları.

Kaynak gerçeği git'teki products.yaml'dır; Telegram'dan yapılan her değişiklik
(ekle/sil/hedef/kaynak/duraklat) ayrı overlay dosyasına yazılır ki kullanıcının
elle düzenlediği dosya (yorumlarıyla birlikte) hiç bozulmasın."""
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import yaml

try:
    import fcntl
    _FLOCK = True
except ImportError:              # Windows — fcntl yok, kilit no-op olur
    _FLOCK = False

# ===================== YOLLAR =====================
BASE_DIR = Path(__file__).resolve().parent.parent
PRODUCTS_YAML = BASE_DIR / "products.yaml"
TELEGRAM_URUNLER = BASE_DIR / "telegram_urunler.yaml"   # Telegram değişiklikleri
KURULUM_YAML = BASE_DIR / "kurulum.yaml"                # kurulum sihirbazı yazar
SITES_YAML = BASE_DIR / "sites.yaml"
STATE_FILE = BASE_DIR / "state.json"
HISTORY_CSV = BASE_DIR / "fiyat_gecmisi.csv"            # eski geçmiş (→ SQLite)
VERI_DB = BASE_DIR / "veri.db"                          # fiyat geçmişi + ürün ID
GRAFIK_HTML = BASE_DIR / "fiyat_grafigi.html"
GRAFIK_PNG = BASE_DIR / "fiyat_grafigi.png"
USER_DATA_DIR = str(BASE_DIR / ".chrome-profile-bot")
LOG_FILE = BASE_DIR / "takip.log"
BOT_LOCK = BASE_DIR / "bot.lock"
# ==================================================


def tek_kopya_kilidi(timeout: float = 50, lock_path: Path = BOT_LOCK):
    """Aynı anda TEK bot çalışsın diye exclusive dosya kilidi (flock).
    Eski instance hâlâ kapanıyorsa (tarayıcı kapatma ~45sn) kilit boşalana kadar
    bekler; timeout dolarsa None döner → çağıran çıkar, gözetmen tekrar dener.
    Kilit process ölünce (çökme dahil) OS tarafından otomatik bırakılır —
    bayat-kilit sorunu yok. Handle DÖNER; çağıran onu process boyunca açık
    tutmalı (kapanınca kilit bırakılır). Windows'ta fcntl yoksa no-op (handle
    döner ama kilitlemez)."""
    f = open(lock_path, "w")
    if not _FLOCK:
        return f
    son = time.time() + timeout
    while True:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            f.write(str(os.getpid()))
            f.flush()
            return f
        except OSError:
            if time.time() >= son:
                f.close()
                return None
            time.sleep(2)


def load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_yaml_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                   encoding="utf-8")
    os.replace(tmp, path)


def product_key(prod: dict) -> str:
    """State anahtarı: etiket (çoklu kaynakta URL tek başına ürünü temsil etmez)."""
    return prod.get("label") or product_urls(prod)[0]


def product_urls(prod: dict) -> list[str]:
    """'urls' listesi varsa onu, yoksa tekil 'url'i döndürür. Sıra önemlidir:
    Akakçe birincil kurgusunda ilk eleman Akakçe linkidir."""
    urls = [u for u in (prod.get("urls") or []) if u]
    return urls or [prod["url"]]


def load_products() -> list[dict]:
    """products.yaml + telegram_urunler.yaml birleşimi. Telegram'dan yapılan
    ekleme/silme/hedef değişikliği ayrı dosyada tutulur ki kullanıcının elle
    düzenlediği products.yaml (yorumlarıyla birlikte) hiç bozulmasın."""
    cfg = load_yaml(PRODUCTS_YAML)
    products = [dict(p) for p in cfg.get("products", []) if p.get("enabled", True)]
    tg = load_yaml(TELEGRAM_URUNLER) if TELEGRAM_URUNLER.exists() else {}
    for p in (tg.get("eklenen") or []):
        products.append(dict(p))
    kaldirilan = set(tg.get("kaldirilan") or [])
    products = [p for p in products if product_key(p) not in kaldirilan]
    hedefler = tg.get("hedefler") or {}
    kaynaklar = tg.get("ek_kaynaklar") or {}
    duraklatilan = set(tg.get("duraklatilan") or [])
    acil = tg.get("acil_hedefler") or {}
    for p in products:
        k = product_key(p)
        if k in hedefler:
            p["price_threshold_tl"] = float(hedefler[k])
        if k in acil:
            p["price_threshold2_tl"] = float(acil[k])
        # /akakce ile bağlanan ek kaynaklar LİSTENİN BAŞINA gelir (Akakçe birincil)
        if k in kaynaklar:
            ek = [u for u in kaynaklar[k] if u]
            p["urls"] = ek + [u for u in product_urls(p) if u not in ek]
        if k in duraklatilan:
            p["paused"] = True     # izleyici başlatılmaz; listede ⏸ görünür
    return products


def _tg_dosya() -> dict:
    d = load_yaml(TELEGRAM_URUNLER) if TELEGRAM_URUNLER.exists() else {}
    d.setdefault("eklenen", [])
    d.setdefault("kaldirilan", [])
    d.setdefault("hedefler", {})
    d.setdefault("ek_kaynaklar", {})
    d.setdefault("duraklatilan", [])
    # setler: {"PC Toplama": {"urunler": [key...], "hedef": 84000.0}}
    d.setdefault("setler", {})
    # acil_hedefler: {key: fiyat} — ikinci eşik (🚨 kısa cooldown, sessiz saati deler)
    d.setdefault("acil_hedefler", {})
    return d


def tg_acil_hedef(key: str, hedef: float | None) -> None:
    d = _tg_dosya()
    if hedef is None:
        d["acil_hedefler"].pop(key, None)
    else:
        d["acil_hedefler"][key] = hedef
    save_yaml_atomic(TELEGRAM_URUNLER, d)


# ===================== SETLER (ürün grupları) =====================

def setleri_getir() -> dict:
    """{set adı: {"urunler": [...], "hedef": float|None}}"""
    return _tg_dosya()["setler"]


def set_urun(set_adi: str, key: str, ekle: bool = True) -> None:
    """Ürünü sete ekler/çıkarır; set yoksa oluşturur, boşalırsa siler."""
    d = _tg_dosya()
    s = d["setler"].setdefault(set_adi, {"urunler": [], "hedef": None})
    if ekle and key not in s["urunler"]:
        s["urunler"].append(key)
    if not ekle and key in s["urunler"]:
        s["urunler"].remove(key)
    if not s["urunler"]:
        d["setler"].pop(set_adi, None)
    save_yaml_atomic(TELEGRAM_URUNLER, d)


def set_hedef(set_adi: str, hedef: float | None) -> None:
    d = _tg_dosya()
    if set_adi in d["setler"]:
        d["setler"][set_adi]["hedef"] = hedef
        save_yaml_atomic(TELEGRAM_URUNLER, d)


def set_sil(set_adi: str) -> None:
    d = _tg_dosya()
    d["setler"].pop(set_adi, None)
    save_yaml_atomic(TELEGRAM_URUNLER, d)


def tg_kaynak_ekle(key: str, url: str) -> None:
    """Ürüne ek kaynak bağlar (Akakçe linki başa gelir → birincil kaynak olur)."""
    d = _tg_dosya()
    lst = d["ek_kaynaklar"].setdefault(key, [])
    if url not in lst:
        lst.insert(0, url)
    save_yaml_atomic(TELEGRAM_URUNLER, d)


def tg_urun_ekle(label: str, url: str, hedef: float) -> dict:
    d = _tg_dosya()
    prod = {"label": label, "url": url, "mode": "price",
            "price_threshold_tl": hedef, "cooldown_minutes": 1440,
            "sleep_min": 900, "sleep_max": 1800}
    d["eklenen"].append(prod)
    if label in d["kaldirilan"]:
        d["kaldirilan"].remove(label)
    save_yaml_atomic(TELEGRAM_URUNLER, d)
    return prod


def tg_urun_sil(key: str) -> None:
    """Telegram'dan eklenen ürünü listeden çıkarır; products.yaml ürünüyse
    'kaldirilan' listesine yazarak devre dışı bırakır (dosyaya dokunmadan)."""
    d = _tg_dosya()
    once = len(d["eklenen"])
    d["eklenen"] = [p for p in d["eklenen"] if product_key(p) != key]
    if len(d["eklenen"]) == once and key not in d["kaldirilan"]:
        d["kaldirilan"].append(key)
    d["hedefler"].pop(key, None)
    d["ek_kaynaklar"].pop(key, None)
    d["acil_hedefler"].pop(key, None)
    if key in d["duraklatilan"]:
        d["duraklatilan"].remove(key)
    # setlerden de düş (boşalan set silinir)
    for ad in list(d["setler"]):
        s = d["setler"][ad]
        if key in s.get("urunler", []):
            s["urunler"].remove(key)
            if not s["urunler"]:
                d["setler"].pop(ad)
    save_yaml_atomic(TELEGRAM_URUNLER, d)


def tg_hedef_degistir(key: str, hedef: float) -> None:
    d = _tg_dosya()
    for p in d["eklenen"]:
        if product_key(p) == key:
            p["price_threshold_tl"] = hedef
            break
    else:
        d["hedefler"][key] = hedef
    save_yaml_atomic(TELEGRAM_URUNLER, d)


def tg_urun_geri_al(key: str) -> None:
    """Silmeyi geri alır: 'kaldirilan'dan çıkarır (products.yaml ürünü) —
    Telegram'dan eklenen ürünün geri alınması arayüz katmanında yapılır
    (silmeden önce ürün kaydı saklanıp tg_urun_ekle_ham ile geri yazılır)."""
    d = _tg_dosya()
    if key in d["kaldirilan"]:
        d["kaldirilan"].remove(key)
    save_yaml_atomic(TELEGRAM_URUNLER, d)


def tg_urun_ekle_ham(prod: dict) -> None:
    """Silme-geri-alma için: ürün kaydını olduğu gibi 'eklenen'e geri koyar."""
    d = _tg_dosya()
    d["eklenen"].append(dict(prod))
    save_yaml_atomic(TELEGRAM_URUNLER, d)


def tg_duraklat(key: str, duraklat: bool) -> None:
    d = _tg_dosya()
    if duraklat and key not in d["duraklatilan"]:
        d["duraklatilan"].append(key)
    if not duraklat and key in d["duraklatilan"]:
        d["duraklatilan"].remove(key)
    save_yaml_atomic(TELEGRAM_URUNLER, d)


def baslik_temizle(t: str) -> str:
    """Sayfa başlığını etikete çevirir: '|' / ' - ' sonrası site adı kırpılır."""
    for ayrac in (" | ", "|", " – ", " — ", " - "):
        if ayrac in t:
            t = t.split(ayrac)[0]
    return " ".join(t.split()).strip()[:60]


def etiket_uret(url: str) -> str:
    """/ekle'de etiket verilmezse URL'den okunaklı bir ad türetir."""
    pr = urlparse(url)
    seg = [s for s in pr.path.split("/") if s]
    ad = seg[-1] if seg else pr.netloc
    ad = re.sub(r"\.(html?|php|aspx?)$", "", ad)
    ad = re.sub(r",\d+$", "", ad)                 # akakce ",1234567" son eki
    ad = re.sub(r"[-_+]", " ", ad)
    ad = re.sub(r"\s+", " ", ad).strip()[:48]
    return ad or pr.netloc


def _tekil_etiket(label: str, products: list) -> str:
    """Aynı etiket varsa '(2)' ekleyerek benzersizleştirir."""
    mevcut = {product_key(p) for p in products}
    if label not in mevcut:
        return label
    i = 2
    while f"{label} ({i})" in mevcut:
        i += 1
    return f"{label} ({i})"


def load_config() -> tuple[dict, list]:
    """Genel ayarlar + ürün listesi. (Site stratejileri tarayici.Sites ile
    ayrıca yüklenir — konfig, tarayıcı katmanına bağımlı olmasın diye.)"""
    cfg = load_yaml(PRODUCTS_YAML)
    settings = dict(cfg.get("settings", {}))
    # Kurulum sihirbazının yazdığı ayarlar (token, chat_id) products.yaml'ı
    # ezmeden ayrı dosyadan gelir
    if KURULUM_YAML.exists():
        settings.update(load_yaml(KURULUM_YAML))
    return settings, load_products()
