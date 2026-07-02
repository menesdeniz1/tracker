# -*- coding: utf-8 -*-
"""TL fiyat ayrıştırma ve biçimleme — botun en kritik saf fonksiyonları."""
import re


def parse_try_amount(s) -> float | None:
    """TL fiyat metnini sayıya çevirir. Kritik ayrım:
    '53.599 TL'    → nokta binlik ayracı  → 53599.0
    '53.599,50 TL' → virgül ondalık       → 53599.5
    '599.5 TL'     → nokta ondalık        → 599.5
    Kural: hem nokta hem virgül varsa SONDAKİ işaret ondalıktır;
    tek işaret varsa ve sonrasında tam 3 hane varsa binliktir."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        v = float(s)
        return v if 0 < v < 100_000_000 else None
    clean = re.sub(r"[^\d.,]", "", str(s))
    if not clean or not any(c.isdigit() for c in clean):
        return None
    if "," in clean and "." in clean:
        if clean.rfind(",") > clean.rfind("."):     # 53.599,50
            clean = clean.replace(".", "").replace(",", ".")
        else:                                       # 53,599.50 (EN yerelli site)
            clean = clean.replace(",", "")
    elif clean.count(",") > 1:                      # 1,053,599
        clean = clean.replace(",", "")
    elif "," in clean:                              # 53599,50 | 53,599
        head, tail = clean.split(",")
        clean = head + tail if len(tail) == 3 else head + "." + tail
    elif clean.count(".") > 1:                      # 1.053.599
        clean = clean.replace(".", "")
    elif "." in clean:                              # 53.599 → binlik | 599.5 → ondalık
        head, tail = clean.split(".")
        if len(tail) == 3:
            clean = head + tail
    try:
        v = float(clean)
    except ValueError:
        return None
    return v if 0 < v < 100_000_000 else None


def tl(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " TL"


def pct(v: float) -> str:
    """7 günlük değişim gösterimi: -4.23 → '↓%4,2', 2.1 → '↑%2,1'."""
    ok = "↓" if v < 0 else "↑"
    return f"{ok}%{abs(v):.1f}".replace(".", ",")
