# 🗺️ Proje Yol Haritası ve Sprint Planlaması

Mail Otomasyon Test Sistemi'nin kurumsal ve eksiksiz bir ürün (SaaS alternatifi) haline gelebilmesi için belirlenen WBS (İş Kırılım Yapısı) analizi. Hedefimiz olan tam otonom test uygulamasına ulaşmak için işleri 3 ana Sprint'e (döneme) böldük.

## 🏃 Sprint 1: Veri Kalıcılığı ve Analitik Dashboard (Tamamlandı)
**Hedef:** Test sonuçlarının dosya tabanlı (CSV) saklanmasından vazgeçilip, SQLite tabanlı kalıcı ve istatistiksel şekilde analizlenebilir yapıya geçmesi. Gelişmiş grafiksel arayüz (Chart.js) inşası.

## 🏃 Sprint 2: Modern Güvenlik ve Otonom Zamanlama (Sıradaki)
**Hedef:** Corporate (Kurumsal) testlerin insan eli değmeden ve en son güvenlik standartlarıyla çalışması.

### Görev Dağılımı:
- **@backend:** 
  - Basic Auth/App Password yerine M365 ve Google Workspace uyumlu **OAuth2 Token/Refresh Token** akışının entegre edilmesi.
  - Python tabanlı bir scheduler (Örn: `APScheduler`) ile "Her sabah saat 08:00'de Matrix'i koştur" mantığının eklenmesi.
- **@devops:** 
  - Bu zamanlayıcı sistemin belleği şişirmemesi (memory leak) için log rotasyon testlerinin yapılması.

## 🏃 Sprint 3: Konteynerizasyon ve Mükemmel UX (User Experience)
**Hedef:** Sistemin herhangi bir cihaza saniyeler içinde kurulabilmesi ve uygulamanın UI hissiyatının premium düzeye çekilmesi.

### Görev Dağılımı:
- **@frontend:**
  - Tasarıma **Dark Mode** (Karanlık Mod) algoritmasının eklenmesi.
  - Panel arası geçişlerde ve test çalışırken "Gerçek zamanlı" iş akışlarında mikro-animasyon (Glassmorphism) iyileştirmelerinin Vanilla CSS ile kodlanması.
- **@devops:**
  - Tüm projenin `Dockerfile` ve `docker-compose.yml` kullanılarak paketlenmesi.
  - Github Actions üzerine CI/CD yazılması.
