# 🗺️ Proje Yol Haritası ve Sprint Planlaması

Mail Otomasyon Test Sistemi'nin kurumsal ve eksiksiz bir ürün (SaaS alternatifi) haline gelebilmesi için belirlenen WBS (İş Kırılım Yapısı) analizi. Hedefimiz olan tam otonom test uygulamasına ulaşmak için işleri 3 ana Sprint'e (döneme) böldük.

## 🏃 Sprint 1: Veri Kalıcılığı ve Analitik Dashboard (Tamamlandı)

**Hedef:** Test sonuçlarının dosya tabanlı (CSV) saklanmasından vazgeçilip, SQLite tabanlı kalıcı ve istatistiksel şekilde analizlenebilir yapıya geçmesi. Gelişmiş grafiksel arayüz (Chart.js) inşası. Ajan ekosisteminin temellerinin atılması.


## 🏃 Sprint 2: İstemci Taklit (Spoofing) ve Detaylı Uyumluluk (Tamamlandı)

**Hedef:** Gerçek dünya e-posta trafiğinin %100 oranında simüle edilmesi. Outlook, Apple Mail, Android Gmail, Thunderbird gibi farklı istemcilerin *X-Mailer*, *Boundary*, *VML/XML* ve HTML render mantıklarının klonlanıp testlerin bu kimliklerle sunucuya atılması. Claude analizlerinin bu spesifik beklentilere göre yapılması.

## 🏃 Sprint 3: Alıcı (Receiver) Simülasyonu ve Headless UI (Tamamlandı)

**Hedef:** Alınan mail'lerin sadece ham veri değil, gerçek tarayıcılar (Playwright) üzerinden görselleştirilerek doğrulanması.
### Görev Dağılımı

- **@backend:**
  - `client_profiles.py` içerisinde Apple Mail, Thunderbird, Android Gmail ve Outlook Desktop için spesifik MIME boundary ve HTML Header kurallarının derinleştirilmesi.
  - Claude analiz prompt'una, "Eğer bu Mail Outlook'tan geldiyse VML tag'leri bozulmuş mu kontrol et" yönergesinin entegrasyonu.
- **@frontend:**
  - `index.html` içerisine kullanıcıların taklit edilecek istemciyi (Sender Client) seçebileceği görselleştirilmiş (ikonlu) bir UI filtresi ve Seçici (Selector) eklenmesi.
- **@devops:** 
  - Sahte istemciler ile yollanan mail'lerin sunucularda Spam filtresine (DKIM/SPF) takılıp takılmadığını simüle edecek SMTP trace loglama ve analiz aracı yapılması.

## 🏃 Sprint 4: Senaryo Genişletme ve Anlamlı İçerik (Tamamlandı)

**Hedef:** Test kapsamının derinleştirilmesi, mesajların anlamlı hale getirilmesi ve Z-Push (iOS) istemci uyumluluğunun önceliklendirildiği faz.

- **@backend:** `message_templates.py` üzerinde 10+ senaryo ve 30+ özgün şablonun oluşturulması. `client_profiles.py` ile iOS EAS (Z-Push) simülasyonunun gerçeklenmesi.
- **@frontend:** Senaryo ve spoofer seçim arayüzünün güncellenmesi.


## 🏃 Sprint 5: Konteynerizasyon ve Raporlama (Tamamlandı)

**Hedef:** Sistemin her ortamda (Docker) çalışabilmesi ve kurumsal rapor çıktılarının (HTML/PDF) üretilmesi. Finallerde Docker build hataları giderildi, volume persistence (SQLite/Rapor) yapılandırıldı.

### Görev Dağılımı (Devam Ediyor)

- **@devops:** Dockerfile ve docker-compose hazırlığı. Playwright bağımlılıklarının imaja dahil edilmesi.
- **@backend:** `reporter.py` üzerinden HTML rapor motorunun yazılması.
- **@frontend:** Dashboard'a "Raporu İndir" butonunun eklenmesi.

## 🏃 Sprint 6: Otonom Test Operasyonu (Sıradaki)

**Hedef:** Sistemin insan müdahalesi olmadan periyodik olarak çalışması ve kritik hatalarda alarm üretmesi.

- **@backend:** APScheduler entegrasyonu ile `main.py`'nin zamanlanmış görev (Cron) olarak koordine edilmesi.
- **@devops:** Hata durumunda Slack, MS Teams veya SMTP üzerinden anlık bildirim sistemi.
- **@frontend:** Dashboard üzerinde "Otomatik Çalışma Takvimi" (Schedule) görselleştirme arayüzü.

## 🏃 Sprint 7: Kurumsal Bağlantı ve Modern Kimlik Doğrulama

**Hedef:** SMTP şifreleri yerine modern API'ler üzerinden (Microsoft Graph, Gmail API) güvenli ve ölçeklenebilir erişim.

- **@backend:** OAuth2 akışının entegrasyonu. Microsoft Graph API ve Google Workspace API adaptörlerinin derinleştirilmesi.
- **@devops:** Token yenileme (Refresh Token) mekanizması ve güvenli Secret yönetimi (Vault/Env).

## 🏃 Sprint 8: Görsel Analiz ve Regresyon Sınaması (Advanced)

**Hedef:** Playwright render sonuçlarının önceki sürümlerle (Baseline) pixel bazlı karşılaştırılması ve görsel bozulmaların (Render Regression) tespiti.

- **@backend:** Görsel karşılaştırma algoritmasının sisteme eklenmesi. Analiz prompt'una "Önceki başarılı test görseliyle karşılaştır" yönergesinin eklenmesi.
- **@frontend:** "Önce/Sonra" (A/B) karşılaştırma modalı ile görsel farkların dashboard'da gösterilmesi.

## 💤 Backlog / Gelecek Fazlar

### Otonom Test Mimarisi & Zamanlama

**Hedef:** OAuth2 Token yapısıyla tam otonom gece testleri yapabilmesi ve testlerin belirli periyotlarla (Cron) otomatik tetiklenmesi. (Beklemeye alındı).
