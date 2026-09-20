# Takip Botu PRO — TR E-Ticaret Fiyat & Stok Takibi (Telegram + Akakçe)

Python price and stock tracker for Turkish e-commerce sites, with Telegram
notifications, price history, and service/update tooling. Site parsers can break
when page layouts change. Supply your own credentials locally; never commit
tokens, chat configuration, or runtime data. Setup instructions below are in Turkish.

Amazon.tr, Hepsiburada, N11, Trendyol, Akakçe, Tebilon vb. sitelerden PC parçası
fiyat/stok takibi yapar, hedef tutunca **Telegram'a** bildirim atar. Aynı ürünü
birden çok kaynaktan (Akakçe birincil) izleyip **en ucuzunu** bildirir.
Ürünler **Telegram'dan yönetilir** (/ekle /sil /hedef), fiyat hedefe inmese bile
**son 30 günün dibini** haber verir, haftada bir **grafik raporu** gönderir.
`git push` ile uzaktan güncellenir; bozuk push otomatik geri alınır (aşağıda).

## Hızlı başlangıç (minimum girdi)

**Windows:** repo'yu klonla, `kur.bat`'a çift tıkla — paketleri kurar, sihirbaz
Telegram'ı soru-cevapla bağlar (token yapıştır + botuna `/start` yaz, o kadar),
istersen botu hemen başlatır. Hiçbir YAML dosyası düzenlemen gerekmez.

**Linux/Mac:**
```bash
git clone https://github.com/menesdeniz1/tracker.git && cd tracker
python3 -m venv venv && venv/bin/pip install -r requirements.txt
venv/bin/playwright install chromium
venv/bin/python takip_botu_pro.py kur   # sihirbaz — token yapıştır + botuna /start yaz
```

Ondan sonrası telefondan: **bota ürün linkini gönder** → fiyatı okur, adı
sayfadan alır, hedefi butonla seçtirir (%3 / %5 / %10 altı ya da kendin yaz).
Bitti — izleme başlar.

Sihirbaz ayarları `kurulum.yaml`'a yazar (git'e girmez); `products.yaml`'ı elle
düzenlemek tamamen isteğe bağlıdır. WhatsApp Web yok: QR yok, oturum derdi yok,
bot **tam headless** çalışır. CallMeBot yedek kanal olarak ayarlanabilir.

## Çalıştırma

```bash
python takip_botu_pro.py          # normal çalışma
python takip_botu_pro.py once     # tüm ürünleri BİR KEZ oku, tabloyu bas, bildirim atma
python takip_botu_pro.py test     # Telegram'a test mesajı
python takip_botu_pro.py chatid   # chat_id'ni öğren
python takip_botu_pro.py grafik   # fiyat_grafigi.html üret (veya: python grafik.py)
```

`once` modu en önemli araçtır: yeni ürün/site eklediğinde önce bununla dene.
Fiyat `—` görünüyorsa veya KAYNAK sütunu `regex` diyorsa `sites.yaml`'a o site için
doğru `price_selector` yaz (sayfada sağ tık → İncele → fiyat elementinin class'ı).

## Telegram'dan yönetim — kartlar ve butonlar (numara ezberi yok)

**`/menu` (veya `/start`) — 🏠 ana menü, her şeyin buton olduğu tek giriş
ekranı.** Bir kez yaz, gerisini butonla yürüt. Her ekranın altında standart
`[⬅️ Geri] [🏠 Menü]` satırı vardır; **geri bağlamsaldır** — bir sete girip
içindeki ürüne dokunduysan geri o sete döner, sorunlu listesinden girdiysen
oraya, 3. sayfadan girdiysen yine 3. sayfaya (hep 1. sayfaya atmaz).

Üç giriş yolu var, hepsi kart/butonlara çıkar:

- **ürün linki gönder** → fiyatı okur, hedefi butonla seçtirir, izleme başlar,
  ardından "Akakçe'ye de bağlayayım mı?" diye kendisi önerir.
- **`/durum`** → özet başlık (`📊 30 ürün · ✅ 24 · ⚠️ 4 · ⏸ 2`) + ürün başına
  tıklanabilir satır (sol: ad, sağ: fiyat/durum — fiyat telefonda hep görünür).
  **Sorunlular en üstte**, hedefe yakınlar hemen altında. Satıra dokun →
  **ürün kartı** açılır: fiyat, hedef (+ hedefe kalan somut TL), 7g trend,
  30g dip, "bu iyi fiyat mı" bağlamı (🟢/🟡/🔴 + 90g dip/medyan), Akakçe
  satıcı sayısı, mini grafik (▁▂▄▆█) ve butonlar: 🎯 hedef değiştir
  (%3/%5/%10 altı ya da elle) · 🚨 acil hedef · 🔍 Akakçe'ye bağla ·
  ⏸ duraklat/devam · ➕ kaynak ekle · 📦 sete ekle · 📈 sadece bu ürünün
  grafiği · 🗑 sil (onaylı + **geri al**'lı).
- **ürün adı yaz** (örn. `kingston`) → kartı doğrudan açılır; birden çok
  eşleşme varsa kısa seçim listesi gelir.

Diğer komutlar: `/sorunlu` (sadece okunamayan/engelliler — her birinde
Akakçe'ye bağlama kısayolu), `/setler`, `/grafik`, `/csv`, `/saglik`,
`/yedek`, `/yardim`. Alarm mesajlarının altında da hızlı aksiyonlar
vardır: **✅ Aldım** (izlemeyi bırakır) · **🔕 1 hafta sustur** ·
**🎯 hedefi değiştir**. Eski numaralı komutlar (`/sil 3`, `/hedef 3 12750`,
`/akakce 3`) geriye uyum için hâlâ çalışır.

**Hedef altında kaldıkça düzenli hatırlatma:** `renotify_minutes` (varsayılan
240 = 4 saat) ayarıyla, fiyat hedefin altında kaldığı sürece bu aralıkla
tekrar bildirim gelir — ürünü silene ya da susturana kadar durmaz. Tek seferlik
bildirim isteniyorsa `renotify_minutes: 0` yapıp ürünün kendi
`cooldown_minutes`'i geçerli olsun.

**📦 Setler (PC toplama):** karttaki *Sete ekle* ile ürünleri grupla
("PC Toplama" gibi) → `/setler`'de **canlı toplam** + dünle kıyas + set hedefi.
Parçalar tek tek hedefte olmasa bile **toplam** set hedefinin altına inince
bildirim gelir; set toplamının zaman grafiği de çizilir.

**Fiyat zekâsı:** alarm ve kartta "bu iyi bir fiyat mı?" bağlamı — 90 günün
dibi/medyanı, günlerin yüzde kaçından ucuz olduğu, tüm zamanların dibi
(🟢 dip bölgesi / 🟡 ortalama altı / 🔴 pahalı dönem) ve Akakçe'de **satıcı
sayısı + 2. en ucuz fiyat** (tek satıcılı şüpheli ucuzluk uyarısıyla).

**🚨 Acil hedef:** hedef menüsünden ikinci bir eşik — altına inince cooldown
beklemez, sessiz saati deler. **Sessiz saatler:** `quiet_hours: "0-8"` ayarıyla
gece normal alarmlar ertelenir (sabah kendiliğinden gelir), ACİL geçer.
Günlük özet artık **değişenler raporu**: sadece düşen/yükselenler + set
toplamları. Ölü linkler (404) ayrıca algılanıp haftada bir hatırlatılır.

Kart gezinmesi mesajı yerinde düzenler — sohbet mesaj çöplüğüne dönmez.
Sadece `telegram_chat_id`'deki sohbetten gelen komutlar işlenir; yabancılar yok sayılır.

Telegram'dan yapılan ekleme/silme/hedef/duraklatma değişiklikleri
`telegram_urunler.yaml`'a yazılır — senin elle düzenlediğin `products.yaml`
hiç bozulmaz; açılışta ikisi birleştirilir. Bot yeniden başlatma gerektirmez,
izleyiciler canlı güncellenir.

## 30 günün en düşüğü sinyali

Sabit hedef bazen fırsat kaçırtır: fiyat hedefe inmese bile **son 30 günün
dibini** gördüğünde bot 📉 bilgi mesajı atar. Günlük minimumlar `state.json`'da
tutulur (CSV taranmaz, hafiftir). Ayarlar: `low30_alert`, `low30_min_days`
(ilk günlerde spam olmasın diye en az 7 günlük veri ister), `low30_cooldown_minutes`.
Hedef alarmı zaten atılacaksa ayrıca dip mesajı atılmaz.

## 7/24 çalıştırma + git push ile uzaktan güncelleme

Botu doğrudan değil, **gözetmeni** çalıştır:

```bash
python3 guncelleyici.py
```

Gözetmen üç iş yapar (ayrıntı: `guncelleyici.py` docstring):

1. **Watchdog** — botu başlatır, çökerse üstel beklemeyle yeniden başlatır.
2. **Otomatik güncelleme** — ~90 sn'de bir git'i yoklar. Sen uzaktan push'larsın;
   gözetmen çeker, `requirements.txt` değiştiyse pip kurar, **smoke testinden**
   geçirir, botu nazikçe yeniden başlatır ve Telegram'a "⬆️ güncellendi" yazar.
3. **Bozuk push koruması** — üç ayrı ağ:
   - Açılmayan/derlenmeyen kod → smoke geçmez, push **geri alınır**, eski sürüm
     hiç kesintiye uğramadan devam eder, telefona "⛔ push bozuk" düşer.
   - Açılıp hemen çöken kod → 3 hızlı çökmeden sonra **bilinen son iyi sürüme**
     otomatik dönülür (5 dk yaşayan sürüm "iyi" işaretlenir).
   - Bot bozuk/kapalı haldeyken düzeltme push'larsan → her yeniden başlatma
     öncesi git kontrol edilir, düzeltme **bir sonraki denemede** alınır.
   Aynı bozuk commit tekrar tekrar denenmez; yeni commit gelince tekrar bakılır.

Katmanlar: işletim sistemi gözetmeni ayakta tutar, gözetmen botu. Gözetmen
bilerek **sadece stdlib** kullanır ve sistem Python'uyla çalışır — venv bozulsa
bile güncelleme mekanizması ölmez. Gözetmenin kendisi de repodan güncellenir
(önce derleme kontrolünden geçer).

**Tek-kopya kilidi:** bot açılışta `bot.lock` üzerinde exclusive dosya kilidi
alır (flock) — yanlışlıkla/yeniden başlatma sırasında ikinci bir kopya asla
aynı anda Telegram'a bağlanamaz (aksi halde iki instance `getUpdates` için
yarışır, butonlar aralıklı cevap verir). Eski kopya kapanana kadar yenisi
bekler; kilit process ölünce (çökme dahil) OS tarafından otomatik bırakılır.

**Zombi motor tespiti:** Playwright'ın arka plan motoru (driver) bazen süreç
canlıyken sessizce ölebilir — bot "çalışıyor" görünür ama ne fiyat okur ne
Telegram'a cevap verir (gözetmen bunu çökme saymaz). Bot artık bu durumu
("pipe closed", "driver kapandı" gibi hata izleriyle) kendisi fark edip
`os._exit(75)` ile kapanır; gözetmen taze bir motorla otomatik yeniden
başlatır. Elle müdahale gerekmez.

**macOS:** `com.takip-botu.plist` içindeki yolları kontrol et, sonra:
```bash
cp com.takip-botu.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.takip-botu.plist
```

**Linux / Raspberry Pi:** `takip-botu.service` şablonunu düzenleyip systemd'ye kur:
```bash
sudo cp takip-botu.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now takip-botu
journalctl -u takip-botu -f     # canlı log
```

**Windows:** `calistir.bat` gözetmeni çalıştırır. Otomatik başlasın:
`Win+R` → `shell:startup` → açılan klasöre `calistir.bat`'ın kısayolunu koy.

ÖNEMLİ: Bu kurguda kaynak gerçeği git'tir — bot makinesinde `products.yaml`
gibi takip edilen dosyaları **elle değiştirme**, değişikliği push'la gönder.
(Telegram komutları serbest: onlar git dışı overlay dosyalarına yazar.)
Elle değişiklik algılanırsa gözetmen güncellemeyi bekletir ve Telegram'a uyarır.

Bot her (yeniden) başlayışta Telegram'a 🔄 mesajı atar; 30 dk içinde arka arkaya
başlıyorsa (çökme döngüsü) mesaj spam'i yapmaz, `takip.log`'a bakman gerektiğini
loglar.

## Akakçe birincil kurgusu (çoklu kaynak)

ÖNEMLİ: Bot ürünleri **kendiliğinden Akakçe'de aramaz** — sadece listedeki
linklere bakar. Bir ürünü Akakçe'ye bağlamanın iki yolu var:

1. **Telefondan (kolay):** ürün kartındaki **🔍 Akakçe'ye bağla** butonu —
   bot ürün adıyla Akakçe'de arar, bulduğu ilk 3 ürün sayfasını buton yapar,
   doğrusuna tıklarsın. O andan itibaren Akakçe o ürünün **birincil** kaynağı
   olur (tüm satıcıların en ucuzu), mevcut mağaza linki yedek kaynak kalır.
   (Yeni ürün eklerken bot bunu kendisi de önerir.)
2. **Elle:** ürüne `url` yerine `urls` listesi ver, ilk sıraya Akakçe linkini koy.

Bot her turda ürünün tüm kaynaklarını kontrol eder ve **en ucuzunu** bildirir;
Akakçe'de en ucuz satıcının adı da bildirime eklenir.

```yaml
- label: "AMD Ryzen 7 7800X3D"
  urls:
    - "https://www.akakce.com/islemci/en-ucuz-amd-ryzen-7-7800x3d-fiyati,XXXXXXX.html"
    - "https://www.amazon.com.tr/dp/B0CJML6LQZ"
  mode: price
  price_threshold_tl: 13500
```

## Dosyalar

| Dosya | Ne işe yarar |
|---|---|
| `takipbotu/` | Bot kodu (fiyat, konfig, veri, tarayici, karar, bildirim, izleyici, arayuz) |
| `takip_botu_pro.py` | İnce giriş noktası — gözetmen ve smoke burayı çağırır |
| `products.yaml` | Ürün listesi + genel ayarlar (Telegram, eşzamanlılık, heartbeat...) |
| `telegram_urunler.yaml` | Telegram değişiklikleri: ekle/sil/hedef/kaynak/duraklat (bot yazar) |
| `sites.yaml` | Siteye özel CSS seçicileri — site okumuyorsa burayı düzelt |
| `veri.db` | Fiyat geçmişi (SQLite, WAL) — eski CSV ilk açılışta içeri aktarılır |
| `grafik.py` | `veri.db` → `fiyat_grafigi.html` (koyu/açık tema, ürün başına grafik) |
| `guncelleyici.py` | Gözetmen: watchdog + git'ten otomatik güncelleme + bozuk push geri alma |
| `com.takip-botu.plist` | macOS launchd şablonu (gözetmeni ayakta tutar) |
| `calistir.bat` | Windows başlatıcı (gözetmeni ayakta tutar) |
| `takip-botu.service` | Linux/RaspberryPi systemd şablonu (gözetmeni ayakta tutar) |
| `guncelleyici_state.json` | Gözetmen durumu: son iyi sürüm + bozuk commit (otomatik) |
| `bot.lock` | Tek-kopya kilidi (otomatik oluşur, boş bırakılabilir) |
| `yedek.zip` | Haftalık otomatik yedek (Telegram'a da gönderilir) |
| `tests/` | pytest birim testleri (parser, karar, göç, veri, grafik, arayüz) |
| `.github/workflows/ci.yml` | CI: ruff + mypy + testler + smoke — hata push anında görünür |

## Testler, CI ve disk disiplini

```bash
venv/bin/pip install -r requirements-dev.txt && venv/bin/python -m pytest
```

Her push GitHub Actions'ta otomatik test edilir (birim testleri + smoke +
gözetmenin Python 3.9 uyumluluğu) — bozuk kod bot makinesine ulaşmadan
kırmızı ✗ olarak görünür; gözetmen ikinci savunma hattıdır.
`requirements.txt` sürümleri bilerek sabittir: yükseltme = sürümü değiştir,
push'la, CI + smoke + canary korur.

Disk asla dolmaz: `takip.log` 5 MB'da döner (en çok ~15 MB), `guncelleyici.log`
2 MB'da döner (~4 MB), tarayıcı profili önbelleği 700 MB'ı aşarsa bot yeniden
başlarken otomatik temizlenir (çerezler/oturumlar korunur;
`TAKIP_PROFIL_LIMIT_MB` ile ayarlanır).

## Otomatik yedekleme + sağlık kontrolü

`veri.db` + `state.json` + `telegram_urunler.yaml` haftada bir (`backup_days`)
zip'lenip **Telegram sohbetine** gönderilir — yedek bilerek makine dışında
durur, disk ölse bile fiyat geçmişin/setlerin/hedeflerin kurtarılabilir.
Elle almak için `/yedek` ya da menüden **🗄 Şimdi yedekle**.

`/saglik` (+ menüde 🩺): ayakta süresi, kaç ürün okunamıyor, veri.db/profil
boyutu, son yedek zamanı, çalışan git commit'i — telefondan öz-teşhis.

## Nasıl karar veriyor? (tasarım kararları)

1. **Fiyat okuma zinciri** — güvenden düşüğe doğru:
   `JSON-LD → siteye özel seçici → genel seçiciler → meta tag → gövde regex`.
   JSON-LD'de `@graph`, `offers`, `lowPrice` gezilir ve para birimi TRY değilse
   reddedilir (bazı siteler USD fiyat da gömer).

2. **TL parser** — `53.599 TL` (nokta=binlik) ile `53.599,50 TL` (virgül=ondalık)
   ayrımı: hem nokta hem virgül varsa *sondaki* işaret ondalıktır; tek işaret varsa
   ve sonrasında tam 3 hane geliyorsa binliktir.

3. **Şüpheli fiyat koruması (sanity guard)** — yanlış alarmın ana kaynağı parse
   hatasıdır. Fiyat şu durumlardan birine giriyorsa *hemen bildirilmez*, 1-2 dk
   sonra ikinci okumayla (±%2 tutarlılık) doğrulanır:
   - kaynak `regex` (düşük güven),
   - hedef fiyatın yarısından da ucuz,
   - son iyi fiyata göre %40'tan fazla ani düşüş **veya %80'den fazla ani
     yükseliş** (bir pazaryeri satıcısının hatalı girdiği fiyatı Akakçe
     "en ucuz" diye gösterebiliyor — kaynak güvenilir olsa da veri hatalı olabilir).

   **Kalıcı-bozuk-kaynak koruması:** yukarıdakinden de aşırısı — regex kaynaklı
   ve hedefin %20'sinden de ucuz, ya da son iyi fiyatın 3 katından fazla —
   normal 2-okuma tutarlılığıyla **asla** otomatik doğrulanmaz. Sebep: bozuk
   bir sayfa/hatalı satıcı listesi kendisiyle saatlerce hatta günlerce
   "tutarlı" kalabilir (iki gerçek vaka: kırık bir link genel kategori
   sayfasına düşüp hep aynı yanlış fiyatı vermişti; bir pazaryeri satıcısı
   günlerce gerçek değerin katları fiyatla listelenmişti). Üç başarısız
   denemeden sonra tek seferlik "kaynak muhtemelen kırık" uyarısı gelir.

4. **Çoklu kaynak + en ucuz seçimi** — ürünün tüm kaynakları okunur, geçerli en
   düşük fiyat bildirime esas olur; her kaynak ayrı ayrı CSV'ye loglanır (grafikte
   siteler ayrı çizgi olur, siteler arası fark görünür).

5. **Mükerrer bildirim engeli** — `state.json` kalıcıdır; bot yeniden başlasa da
   cooldown (varsayılan 24 sa) devam eder. Ama cooldown içinde fiyat
   `renotify_drop_pct` (%3) kadar *daha da* düşerse yine bildirir.

6. **Site bazlı kuyruk + geri çekilme** — aynı siteye istekler serileştirilir
   (`min_gap_per_host_seconds` + jitter). Captcha algılanırsa o siteye üstel geri
   çekilme (5 dk → 60 dk), başarılı okumada sıfırlanır.

7. **Bildirim güvenilirliği** — Telegram 3 deneme + üstel bekleme; hepsi patlarsa
   CallMeBot yedeğine düşer; o da patlarsa ERROR log. Sessiz kayıp yok.

8. **Sessiz ölüm koruması** — günlük heartbeat artık tam liste değil
   **değişenler raporu** (son 24 saatte düşen/yükselen top 5 + set toplamları
   + okunamayan sayısı), 12 saatten eski okumalara ⚠️ işareti, üst üste 5
   başarısız okumada tek seferlik uyarı. Fiyatın hiç okunamadığı turlar da
   hata serisine sayılır. Süreç canlıyken motor sessizce ölürse (zombi durum)
   bot bunu kendisi fark edip kapanır, gözetmen taze başlatır (yukarıda).

9. **RAM disiplini** — her kontrol kendi sekmesini açar ve `finally` ile kapatır.

10. **Etiket değişikliğinde geçmiş göçü** — state/CSV/hedefler etikete göre
    anahtarlıdır. Ürünü `products.yaml`'da yeniden adlandırırsan bot açılışta
    URL parmak iziyle eski kaydı bulur ve geçmişi (cooldown, günlük minimumlar,
    grafik verisi, hedef/ek-kaynak) yeni ada taşır — hiçbir şey sıfırlanmaz.
    Not: etiketi VE tüm linkleri aynı anda değiştirirsen eşleşme yapılamaz.

## Sorun giderme

| Belirti | Çözüm |
|---|---|
| Fiyat hep `—` | `once` çalıştır; fiyat elementinin class'ını `sites.yaml`'a yaz |
| Kaynak `regex` | Çalışır ama güven düşük; siteye özel seçici ekle |
| `[ENGEL]` / `⛔` | Site bot koruması gösteriyor; bot kendini geri çeker. Sık oluyorsa `sleep_min/max` büyüt |
| Telegram gitmiyor | `test` çalıştır; token ve chat_id'yi kontrol et |
| Bot ölmüş, haberim yok | `heartbeat_hour` ayarlı olsun; istersen `callmebot_apikey` da doldur |
| Grafik boş | Bot en az bir fiyat okumuş olmalı (`veri.db` oluşmalı) |
