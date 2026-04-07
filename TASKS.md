# 📋 Proje İş Takibi (Tasks)

## 🏃 Sprint 1: Veri Kalıcılığı ve Analitik Dashboard
**Durum:** Tamamlandı

## 🏃 Sprint 2: İstemci Taklit (Spoofing) ve Detaylı Uyumluluk
**Durum:** Başlamak Üzere
**PM Notu:** Müşterinin talebi üzerine bu Sprint, uygulamanın farklı Mail istemcilerini (Outlook, Apple Mail, Gmail mobil vb.) kusursuz şekilde simüle etmesi ve uyumluluk (Render/MIME) testlerinin gerçeklenmesine ayrılmıştır.

### 💻 @backend Görevleri
- `[x]` **1. Profil Oluşturucu (Spoofing Engine)**
  - `[x]` `client_profiles.py` içerisinde "Thunderbird", "Apple Mail" ve "Android Gmail" için X-Mailer, spesifik MIME Multipart boundary tag yapıları kodlanacak.
- `[x]` **2. VML/XML Enjeksiyonu**
  - `[x]` Eğer gönderici (Sender) "Outlook" olarak seçilmişse, HTML body içerisine Microsoft'a özgü `<!--[if mso]>` VML ve XML tag'leri enjekte edilecek.
- `[x]` **3. Claude Uyumluluk Analizi**
  - `[x]` `analyzer.py` içerisindeki Prompt güncellenerek: "Eğer alıcı istemci Apple Mail ise resimleri inline değil attachment gibi gösterebilir, bunu hata sayma" gibi akıllı uyumluluk/render kuralları eklenecek.

### 🎨 @frontend Görevleri
- `[x]` **1. İstemci Seçici Arayüz (Mockup/UI)**
  - `[x]` `templates/index.html` dosyasında bulunan "Test Matrisi" kısmına veya "Dashboard" içine kullanıcıların testin gönderici cihazını ve alıcı cihazını ikonlarla seçebileceği (📱 iOS, 💻 Windows Outlook) modern bir UI tasarlanacak.

### 🛠️ @devops Görevleri
- `[x]` **1. Spam/SMTP İzin Trace**
  - `[x]` Spoof edilen mailer'lar gerçek olmadığı için SMTP sunucusundan (EMS vb.) dönebilecek olası Reject/Spam hatalarını detaylı flag'leyecek Log altyapısı yazılacak.

## 🎯 SPİRNT 3: Alıcı (Receiver) Simülasyonu Katmanları ve Headless UI (PM Onay Bekliyor)
Bu sprint, alınan e-postaların IMAP harici yerel API erişimleri (Graph API, EWS) ve gerçek görünmez tarayıcılarla arayüzde (Chrome/Playwright) render edilip piksellerine kadar doğrulanmasını kapsar. Aynı zamanda Gemini API'nin yapılandırılması tamamlanmıştır.

### 💻 @backend Görevleri
- `[x]` **1. Özel Erişim Adaptörleri**
  - `[x]` Microsoft Graph API ve EWS için gelen kutusu erişim yapıları.
- `[x]` **2. Headless Browser (Playwright) Altyapısı**
  - `[x]` Gelen mailin alıcı Web istemcisinde nasıl göründüğünü denetleyen UI-rendering test kodu başlatılacak.

### 🎨 @frontend Görevleri
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

## 🤖 Faz 5: Konteynerizasyon ve Otonom Zamanlama
*Planlanan.*
