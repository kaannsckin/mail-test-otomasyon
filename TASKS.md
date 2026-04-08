# 📋 Proje İş Takibi (Tasks)

## 🏃 Sprint 1: Veri Kalıcılığı ve Analitik Dashboard

**Durum:** ✅ Tamamlandı

## 🏃 Sprint 2: İstemci Taklit (Spoofing) ve Detaylı Uyumluluk

**Durum:** ✅ Tamamlandı

**PM Notu:** Müşterinin talebi üzerine bu Sprint, uygulamanın farklı Mail istemcilerini (Outlook, Apple Mail, Gmail mobil vb.) kusursuz şekilde simüle etmesi ve uyumluluk (Render/MIME) testlerinin gerçeklenmesine ayrılmıştır.

### 💻 @backend Görevleri (S2)

- `[x]` **1. Profil Oluşturucu (Spoofing Engine)**
  - `[x]` `client_profiles.py` içerisinde "Thunderbird", "Apple Mail" ve "Android Gmail" için X-Mailer, spesifik MIME Multipart boundary tag yapıları kodlanacak.
- `[x]` **2. VML/XML Enjeksiyonu**
  - `[x]` Eğer gönderici (Sender) "Outlook" olarak seçilmişse, HTML body içerisine Microsoft'a özgü `<!--[if mso]>` VML ve XML tag'leri enjekte edilecek.
- `[x]` **3. Claude Uyumluluk Analizi**
  - `[x]` `analyzer.py` içerisindeki Prompt güncellenerek: "Eğer alıcı istemci Apple Mail ise resimleri inline değil attachment gibi gösterebilir, bunu hata sayma" gibi akıllı uyumluluk/render kuralları eklenecek.

### 🎨 @frontend Görevleri (S2)

- `[x]` **1. İstemci Seçici Arayüz (Mockup/UI)**
  - `[x]` `templates/index.html` dosyasında bulunan "Test Matrisi" kısmına veya "Dashboard" içine kullanıcıların testin gönderici cihazını ve alıcı cihazını ikonlarla seçebileceği (📱 iOS, 💻 Windows Outlook) modern bir UI tasarlanacak.

### 🛠️ @devops Görevleri (S2)

- `[x]` **1. Spam/SMTP İzin Trace**
  - `[x]` Spoof edilen mailer'lar gerçek olmadığı için SMTP sunucusundan (EMS vb.) dönebilecek olası Reject/Spam hatalarını detaylı flag'leyecek Log altyapısı yazılacak.

## 🏃 Sprint 3: Alıcı (Receiver) Simülasyonu ve Headless UI

**Durum:** ✅ Tamamlandı (08.04.2026)

Bu sprint, alınan e-postaların IMAP harici yerel API erişimleri (Graph API, EWS) ve gerçek görünmez tarayıcılarla arayüzde (Chrome/Playwright) render edilip piksellerine kadar doğrulanmasını kapsar. Aynı zamanda Gemini API'nin yapılandırılması tamamlanmıştır.

### 💻 @backend Görevleri (S3)

- `[x]` **1. Özel Erişim Adaptörleri**
  - `[x]` Microsoft Graph API ve EWS için gelen kutusu erişim yapıları.
- `[x]` **2. Headless Browser (Playwright) Altyapısı**
  - `[x]` Gelen mailin alıcı Web istemcisinde nasıl göründüğünü denetleyen UI-rendering test kodu başlatılacak.

### 🎨 @frontend Görevleri (S3)

- `[x]` **1. Ekran Görüntüsü (Screenshot) Modal Entegrasyonu**
  - `[x]` Dashboard üzerinde, arka planda (Playwright ile) alınan alıcı ekran render testlerinin visual kanıtları basılacak.

## 🏃 Sprint 4: Senaryo Genişletme ve Anlamlı İçerik (Tamamlandı)
**Durum:** Tamamlandı (07.04.2026)
**PM Notu:** Test kapsamının derinleştirilmesi, mesajların anlamlı hale getirilmesi ve Z-Push (iOS) istemci uyumluluğunun önceliklendirildiği faz.

### 💻 @backend Görevleri
- `[x]` **1. Senaryo Çeşitliliği (Matrix Expansion)**
  - `[x]` 10 farklı senaryo (Table, Forward Chain, Multi-attachment vb.) için 30'dan fazla özgün şablon oluşturuldu.
- `[x]` **2. Z-Push (iOS EAS) Profilleme**
  - `[x]` `client_profiles.py` içerisinde iOS cihazların EAS üzerinden gönderdiği spesifik MIME ve Header yapıları klonlandı.
- `[x]` **3. Otomatik Doğrulama Kuralları**
  - `[x]` `analyzer.py` içerisinde yeni eklenen tablo ve zincirleme mesaj yapılarını denetleyen kural seti aktif edildi.

### 🎨 @frontend Görevleri
- `[x]` **1. UI Entegrasyonu**
  - `[x]` `index.html` üzerinde yeni senaryolar için seçim butonları ve Z-Push spoofer seçeneği eklendi.

## 🏃 Sprint 5: Konteynerizasyon ve Raporlama

**Durum:** ✅ Tamamlandı (08.04.2026)

### 🛠️ @devops Görevleri (S5)

- `[x]` **1. Dockerize Etme**
  - `[x]` `Dockerfile` oluşturulması (Playwright/Python bağımlılıkları dahil).
  - `[x]` `docker-compose.yml` ile tek komutla kurulum desteği.
  - `[x]` SQLite veritabanı için volume persistency yapılandırması.

### 💻 @backend Görevleri (S5)

- `[x]` **1. HTML Rapor Motoru**
  - `[x]` `reporter.py`'ye interaktif HTML rapor export özelliği eklenmesi (Jinja2).
- `[x]` **2. SMTP Trace Loglama**
  - `[x]` SMTP reddedilme hatalarının dashboard loglarına detaylı yansıtılması (SPOOF_BLOCK).

---

## 🏃 Sprint 6: Otonom Test Operasyonu
**Durum:** Başlamak Üzere

### 💻 @backend Görevleri
- `[ ]` **1. APScheduler Entegrasyonu**
  - `[ ]` Arka planda `main.py`'yi periyodik (Cron veya Interval) tetikleyecek servis yazılması.
- `[ ]` **2. Bildirim (Alerting) Sistemi**
  - `[ ]` Test fail olduğunda Slack/Teams/Email üzerinden anlık uyarı mekanizması.

### 🎨 @frontend Görevleri
- `[ ]` **1. Otomasyon Paneli**
  - `[ ]` Schedule ayarlarının ve bekleyen otonom testlerin izlenebileceği UI.

## 🏃 Sprint 7: Kurumsal Bağlantı (OAuth2)
**Durum:** Planlama Aşamasında

### 💻 @backend Görevleri
- `[ ]` **1. MS Graph / Google API Adaptörleri**
  - `[ ]` Modern authentication ve token yönetimi (Refresh Token) modülleri.

## 🏃 Sprint 8: Görsel Regresyon
**Durum:** Taslak

### 💻 @backend Görevleri
- `[ ]` **1. Pixel-Match Analizörü**
  - `[ ]` Kayıtlı başarılı (Baseline) screenshot ile yeni renderın karşılaştırılması.
