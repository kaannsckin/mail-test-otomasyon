# 🗺️ Proje Yol Haritası ve Sprint Planlaması

Mail Otomasyon Test Sistemi'nin kurumsal ve eksiksiz bir ürün (SaaS alternatifi) haline gelebilmesi için belirlenen WBS (İş Kırılım Yapısı) analizi. Hedefimiz olan tam otonom test uygulamasına ulaşmak için işleri 3 ana Sprint'e (döneme) böldük.

## 🏃 Sprint 1: Veri Kalıcılığı ve Analitik Dashboard (Tamamlandı)
**Hedef:** Test sonuçlarının dosya tabanlı (CSV) saklanmasından vazgeçilip, SQLite tabanlı kalıcı ve istatistiksel şekilde analizlenebilir yapıya geçmesi. Gelişmiş grafiksel arayüz (Chart.js) inşası. Ajan ekosisteminin temellerinin atılması.

## 🏃 Sprint 2: İstemci Taklit (Spoofing) ve Detaylı Uyumluluk (Sıradaki)
**Hedef:** Gerçek dünya e-posta trafiğinin %100 oranında simüle edilmesi. Outlook, Apple Mail, Android Gmail, Thunderbird gibi farklı istemcilerin *X-Mailer*, *Boundary*, *VML/XML* ve HTML render mantıklarının klonlanıp testlerin bu kimliklerle sunucuya atılması. Claude analizlerinin bu spesifik beklentilere göre yapılması.

### Görev Dağılımı:
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

## 🏃 Sprint 5: Konteynerizasyon ve Otonom Test Mimarisi (Opsiyonel / İleri Faz)
**Hedef:** Sistemin Github Actions/Docker ile her ortama anında kurulabilmesi ve OAuth2 Token yapısıyla tam otonom gece testleri yapabilmesi.
