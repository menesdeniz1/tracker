# -*- coding: utf-8 -*-
"""Playwright katmanı: stealth, site stratejileri, host kuyruğu, sayfa okuma.

Fiyat okuma zinciri güvenden düşüğe: JSON-LD → siteye özel seçici → genel
seçiciler → meta tag → gövde regex (text_scope ile daraltılmış). Dönen
'kaynak' alanı güven seviyesidir; 'regex' düşük güvendir ve karar katmanı
bildirimden önce ikinci okumayla doğrular."""
import asyncio
import json
import logging
import random
import re
import time
from collections import defaultdict
from urllib.parse import quote_plus, urlparse

from playwright.async_api import BrowserContext, Page
from playwright.async_api import TimeoutError as PWTimeout

from . import konfig
from .fiyat import parse_try_amount

# Bot koruması / captcha sayfası işaretleri (başlık + gövdenin ilk kısmında aranır)
BLOCK_MARKERS = [
    "robot check", "captcha", "erişim engellendi", "access denied",
    "olağandışı trafik", "unusual traffic", "attention required",
    "checking your browser", "doğrulama gerekiyor",
    "just a moment",             # Cloudflare ara sayfası (tebilon vb.)
    "hepsiburada | güvenlik",    # Hepsiburada bot duvarı (başlık)
]

AKAKCE_ARAMA = "https://www.akakce.com/arama/?q={}"


class Sites:
    def __init__(self, raw: dict):
        self.sites = raw.get("sites", {})
        self.default = self.sites.get("default", {})

    def strategy(self, host: str) -> dict:
        for domain, strat in self.sites.items():
            if domain != "default" and domain in host:
                return {**self.default, **(strat or {})}
        return dict(self.default)


async def apply_stealth(page: Page) -> None:
    await page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.chrome = { runtime: {} };
        Object.defineProperty(navigator, 'languages', { get: () => ['tr-TR','tr','en-US','en'] });
        Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
    """)
    await page.set_extra_http_headers({
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
        "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="124", "Google Chrome";v="124"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    })


async def launch_context(p, headless: bool) -> BrowserContext:
    return await p.chromium.launch_persistent_context(
        konfig.USER_DATA_DIR,
        headless=headless,
        viewport={"width": 1280, "height": 800},
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    )


# ===================== SİTE BAZLI KUYRUK + GERİ ÇEKİLME =====================

class HostThrottle:
    """Aynı siteye istekleri sıralar: 4 Amazon ürünü aynı anda gitmez, aralarında
    min_gap + jitter olur. Captcha/engel görülürse o siteye üstel geri çekilme."""

    def __init__(self, min_gap: float = 25.0):
        self.min_gap = min_gap
        self.locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.next_ok: dict[str, float] = defaultdict(float)      # monotonic zaman
        self.backoff: dict[str, float] = defaultdict(float)      # saniye

    def slot(self, host: str):
        return _ThrottleSlot(self, host)

    def penalize(self, host: str) -> float:
        """Engel algılandı → geri çekilmeyi büyüt (5 dk → 10 → ... → 60 dk)."""
        self.backoff[host] = min(max(self.backoff[host] * 2, 300), 3600)
        self.next_ok[host] = time.monotonic() + self.backoff[host]
        return self.backoff[host]

    def reward(self, host: str) -> None:
        """Başarılı okuma → geri çekilmeyi sıfırla."""
        self.backoff[host] = 0.0


class _ThrottleSlot:
    def __init__(self, throttle: HostThrottle, host: str):
        self.t = throttle
        self.host = host

    async def __aenter__(self):
        await self.t.locks[self.host].acquire()
        wait = self.t.next_ok[self.host] - time.monotonic()
        if wait > 0:
            logging.debug(f"[{self.host}] site kuyruğu: {wait:.0f} sn bekleniyor")
            await asyncio.sleep(wait)
        return self

    async def __aexit__(self, *exc):
        # bir sonraki aynı-site isteği için minimum aralık + jitter
        gap = self.t.min_gap * random.uniform(0.8, 1.6)
        self.t.next_ok[self.host] = max(
            self.t.next_ok[self.host], time.monotonic() + gap)
        self.t.locks[self.host].release()
        return False


# ===================== SAYFA OKUMA =====================

def _jsonld_iter(node):
    """JSON-LD içinde gezinir: listeler, @graph, iç içe yapılar.
    'object': mediamarkt gibi siteler Product'ı BuyAction.object içine gömer."""
    if isinstance(node, list):
        for x in node:
            yield from _jsonld_iter(x)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "mainEntity", "itemListElement", "item", "offers",
                    "object"):
            if key in node:
                yield from _jsonld_iter(node[key])


def _price_from_jsonld_obj(obj: dict) -> float | None:
    """Bir JSON-LD düğümünden fiyat çeker. TRY dışındaki para birimlerini reddeder
    (bazı siteler USD fiyat da gömer)."""
    for key in ("price", "lowPrice"):
        raw = obj.get(key)
        if raw is None:
            continue
        cur = str(obj.get("priceCurrency") or "").upper()
        if cur and cur not in ("TRY", "TL"):
            continue
        v = parse_try_amount(raw)
        if v:
            return v
    return None


async def get_price(page: Page, strat: dict) -> tuple[float | None, str]:
    """Öncelik: JSON-LD → siteye özel seçici → genel seçiciler → meta → gövde regex.
    Dönen 'kaynak' alanı güven seviyesidir; 'regex' düşük güven demektir ve
    bildirimden önce ikinci okumayla doğrulanır."""
    # 1) JSON-LD (en güvenilir: sitenin kendi yapılandırılmış verisi)
    try:
        for s in await page.locator('script[type="application/ld+json"]').all():
            try:
                data = json.loads(await s.inner_text())
            except Exception:
                continue
            for obj in _jsonld_iter(data):
                # Ana ürün düğümlerine öncelik: Product tipi veya offers taşıyan düğüm
                t = str(obj.get("@type") or "")
                if "Product" in t or "Offer" in t or "offers" in obj:
                    v = _price_from_jsonld_obj(obj)
                    if v:
                        return v, "json-ld"
    except Exception:
        pass

    # 2) Siteye özel + genel seçiciler
    adaylar = []
    if strat.get("price_selector"):
        adaylar.append(strat["price_selector"])
    adaylar += [".product-price", "#product-price", ".price-value", "span.price",
                ".current-price", "[data-price-amount]", "span[itemprop=price]"]
    for sel in adaylar:
        try:
            locs = page.locator(sel)
            for i in range(min(await locs.count(), 5)):
                el = locs.nth(i)
                if not await el.is_visible():
                    continue
                v = parse_try_amount(await el.inner_text())
                if v:
                    return v, "seçici"
                v = parse_try_amount(await el.get_attribute("content"))
                if v:
                    return v, "seçici"
        except Exception:
            continue

    # 3) Meta tag'ler
    for meta_sel in ('meta[property="product:price:amount"]',
                     'meta[property="og:price:amount"]',
                     'meta[itemprop="price"]'):
        try:
            meta = await page.locator(meta_sel).first.get_attribute("content", timeout=1500)
            v = parse_try_amount(meta)
            if v:
                return v, "meta"
        except Exception:
            continue

    # 4) Son çare: gövdedeki ilk '12.345,67 TL' kalıbı — DÜŞÜK GÜVEN
    # (önerilen ürün fiyatını yakalayabilir; bu yüzden bildirim öncesi doğrulanır)
    # sites.yaml → text_scope: regex'in bakacağı alanı daraltır (örn. Amazon'da
    # "#centerCol" — sponsorlu ürün karuselinin fiyatını ana fiyat sanmasın).
    # Kapsam elementi sayfada yoksa regex adımı ATLANIR: fiyatsız sayfada
    # (stok yok vb.) yanlış fiyat okumaktansa "yok" demek daha doğru.
    try:
        scope = strat.get("text_scope")
        body = await page.locator(scope or "body").first.inner_text(timeout=3000)
        m = re.search(r"(\d{1,3}(?:\.\d{3})+(?:,\d{2})?|\d+,\d{2})\s*(?:TL|₺)", body)
        if m:
            logging.debug("Fiyat gövde regex ile bulundu (düşük güven).")
            return parse_try_amount(m.group(1)), "regex"
    except Exception:
        pass
    return None, "yok"


async def get_seller(page: Page, strat: dict) -> str | None:
    """Akakçe gibi karşılaştırma sitelerinde en ucuz satıcının adını çeker.
    sites.yaml → seller_selector; bulunamazsa sessizce None."""
    sel = strat.get("seller_selector")
    if not sel:
        return None
    try:
        el = page.locator(sel).first
        if await el.count() == 0:
            return None
        text = (await el.inner_text() or "").strip()
        if not text:
            for attr in ("alt", "title", "aria-label"):
                text = (await el.get_attribute(attr) or "").strip()
                if text:
                    break
        text = " ".join(text.split())
        return text[:60] if text else None
    except Exception:
        return None


async def _akakce_sonuc_ayikla(page: Page) -> list[dict]:
    """Açık Akakçe arama sayfasından ürün sayfası linklerini toplar.
    Sayfa yapısına değil, Akakçe'nin değişmez URL kalıbına dayanır:
    ürün sayfaları daima '...-fiyati,ID.html' biçimindedir."""
    out, gorulen = [], set()
    anchors = page.locator("a[href*='fiyati,']")
    for i in range(min(await anchors.count(), 25)):
        try:
            a = anchors.nth(i)
            href = await a.get_attribute("href") or ""
            if not re.search(r"fiyati,\d+\.html", href):
                continue
            if href.startswith("/"):
                href = "https://www.akakce.com" + href
            href = href.split("?")[0]
            if href in gorulen:
                continue
            ad = " ".join(((await a.inner_text()) or "").split()).strip()
            if not ad:
                ad = (await a.get_attribute("title") or "").strip()
            if not ad:
                continue
            gorulen.add(href)
            out.append({"ad": ad[:70], "url": href})
            if len(out) == 3:
                break
        except Exception:
            continue
    return out


async def akakce_ara(context: BrowserContext, throttle: HostThrottle,
                     query: str, arama_url: str | None = None) -> list[dict]:
    """Akakçe'de ürün adıyla arar, ilk 3 ürün sayfasını döndürür.
    Arayüz bunları buton yapar, kullanıcı doğrusunu seçer."""
    url = arama_url or AKAKCE_ARAMA.format(quote_plus(query))
    host = urlparse(url).netloc or "www.akakce.com"
    async with throttle.slot(host):
        page = await context.new_page()
        try:
            await apply_stealth(page)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except PWTimeout:
                pass
            await asyncio.sleep(2.5)
            return await _akakce_sonuc_ayikla(page)
        finally:
            await page.close()


async def detect_block(page: Page) -> bool:
    """Captcha / bot koruması / erişim engeli sayfası mı?"""
    try:
        title = (await page.title() or "").lower()
        if any(m in title for m in BLOCK_MARKERS):
            return True
        body = (await page.locator("body").inner_text(timeout=2000))[:3000].lower()
        return any(m in body for m in BLOCK_MARKERS)
    except Exception:
        return False


async def check_add_to_cart(page: Page, strat: dict) -> bool | None:
    """True=stokta, False=pasif/tükendi, None=buton hiç bulunamadı (bilinmiyor)."""
    seciciler = [strat.get("add_button") or "button:has-text('Sepete Ekle')",
                 "button:has-text('SEPETE EKLE')", "button:has-text('Add to Cart')",
                 "#add-to-cart-button", "button.add-to-basket", "button.add-to-cart"]
    btn = None
    for sel in seciciler:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                btn = loc
                break
        except Exception:
            continue
    if btn is None:
        return None
    try:
        if not await btn.is_visible():
            return False
        if await btn.is_disabled():
            return False
        cls = (await btn.get_attribute("class") or "").lower()
        if any(x in cls for x in ["disabled", "passive", "sold-out", "out-of-stock",
                                  "unavailable", "not-available"]):
            return False
        return True
    except Exception:
        return None


async def select_variant(page: Page, targets: list, strat: dict) -> tuple[bool, str]:
    """Beden/varyant seçer. ['*'] veya boşsa atlanır. Pasif (tükendi) seçenekleri eler."""
    if not targets or "*" in targets or "ANY" in targets:
        return True, "AUTO"
    hedefler = [str(t).upper() for t in targets]

    trigger = strat.get("size_trigger")
    if trigger:
        try:
            t = page.locator(trigger).first
            if await t.count() > 0 and await t.is_visible():
                await t.click()
                await asyncio.sleep(1.2)
        except Exception:
            pass

    seciciler = [strat.get("size_items") or "button, li, [role='button']",
                 "button", "li", "span", "[data-variant-name]"]
    for sel in seciciler:
        try:
            locs = page.locator(sel)
            for i in range(min(await locs.count(), 80)):
                el = locs.nth(i)
                try:
                    if not await el.is_visible():
                        continue
                    text = (await el.inner_text()).strip()
                    if not text:
                        for attr in ("title", "data-value", "aria-label", "value"):
                            text = (await el.get_attribute(attr) or "").strip()
                            if text:
                                break
                    if not text or len(text) > 40:
                        continue
                    tu = text.upper()
                    eslesme = any(
                        h == tu or f" {h} " in f" {tu} "
                        or (h.replace(".", "").isdigit() and h == re.sub(r"[^\d.]", "", tu))
                        for h in hedefler
                    )
                    if not eslesme:
                        continue
                    cls = (await el.get_attribute("class") or "").lower()
                    if any(x in cls for x in ["disabled", "sold-out", "passive",
                                              "out-of-stock", "unavailable", "tukendi"]):
                        continue
                    if await el.is_enabled():
                        await el.scroll_into_view_if_needed()
                        await el.click(force=True, delay=random.randint(50, 150))
                        await asyncio.sleep(1)
                        return True, text
                except Exception:
                    continue
        except Exception:
            continue
    return False, ""


async def check_once(context: BrowserContext, sites: Sites, throttle: HostThrottle,
                     prod: dict, url: str) -> dict:
    """Tek kaynağın tek kontrolü. Site kuyruğuna girer, sekme açar, okur, KAPATIR."""
    host = urlparse(url).netloc
    strat = sites.strategy(host)
    sonuc: dict = {"url": url, "host": host.replace("www.", ""),
             "price": None, "source": "yok", "seller": None, "title": "",
             "in_stock": None, "variant_ok": True, "variant": "AUTO", "blocked": False}

    async with throttle.slot(host):
        page = await context.new_page()
        try:
            await apply_stealth(page)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except PWTimeout:
                logging.warning(f"[{prod.get('label')}] sayfa yükleme zaman aşımı, "
                                "mevcut haliyle okunuyor...")
            await page.mouse.move(random.randint(80, 500), random.randint(80, 500))
            await asyncio.sleep(strat.get("wait_after_page_load", 3))
            try:
                sonuc["title"] = (await page.title() or "").strip()
            except Exception:
                pass

            if await detect_block(page):
                sonuc["blocked"] = True
                ceza = throttle.penalize(host)
                logging.warning(f"[{prod.get('label')}] {host} bot koruması algılandı! "
                                f"Bu siteye {ceza/60:.0f} dk geri çekilme uygulanıyor.")
                return sonuc

            mode = prod.get("mode", "price")
            if "stock" in mode:
                sonuc["variant_ok"], sonuc["variant"] = await select_variant(
                    page, prod.get("target_variants", ["*"]), strat)
                sonuc["in_stock"] = await check_add_to_cart(page, strat)
            if "price" in mode:
                sonuc["price"], sonuc["source"] = await get_price(page, strat)
                sonuc["seller"] = await get_seller(page, strat)
            throttle.reward(host)
        finally:
            await page.close()
    return sonuc
