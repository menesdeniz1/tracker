# -*- coding: utf-8 -*-
"""Takip Botu PRO — modül paketi.

Katmanlar (alttan üste, ok bağımlılık yönü):
  fiyat     → TL ayrıştırma/biçimleme (saf fonksiyonlar)
  konfig    → yollar, YAML, ürün listesi birleşimi, etiket yardımcıları
  veri      → state.json, fiyat geçmişi, günlük minimumlar, etiket göçü
  tarayici  → Playwright: stealth, site stratejileri, host kuyruğu, sayfa okuma
  karar     → alarm gerekli mi, fiyat şüpheli mi, hangi kaynak esas
  bildirim  → Telegram (+ CallMeBot yedeği)
  izleyici  → ürün izleme döngüsü, canlı yönetici, heartbeat
  arayuz    → Telegram komutları/butonları

Giriş noktası kökteki takip_botu_pro.py'dir (gözetmen ve smoke oradan çalışır).
"""
