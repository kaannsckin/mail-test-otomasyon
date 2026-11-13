# Ürün Yol Haritası — Mail Regresyon Test Aracı

Amaç: bu projeyi, bir kurumun **kurup yapılandırıp çalıştırabileceği** ve
mesajlaşma ürünü ya da istemci güncellemelerinde "mesaj alma/gönderme
bozulmadı" iddiasını **kanıtlayabileceği** bir ürüne dönüştürmek.

Bu belge bir tartışma taslağıdır; fazların sırası değişebilir.

## Alınan kararlar

- **Ortam:** Android → Gmail uygulaması, IMAP + SMTP. iOS/iPadOS → Z-Push
  (son sürüm, arkasında EMS IMAP). **Outlook masaüstü kritik**; çoğunluk
  POP3 + SMTP, bir kısmı IMAP.
- **Cihaz:** iPad Pro var (manuel doğrulama için yeterli); Mac yok → iOS
  cihaz otomasyonu (L3) kapsam dışı.
- **LLM:** hem LLM'li hem LLM'siz kip sunulur.
- **Hedef:** kurum içi kullanım → çok kiracılı yapı, lisanslama, EN arayüz plandan çıktı.
- **Kombinasyonlar:** 18 satırlık CSV yerine test planı (yol · istemci · kademe ·
  etki) — uygulandı, bkz. README → Test Planı. Gmail ve Outlook/O365 dış
  dünyayı temsil eder; ağırlık EMS ↔ EMS.

## Hedef: e-posta sisteminin son kullanıcı testleri, az testçiyle

Araç EMS'e **kullanıcının girdiği kapılardan** girer (SMTP, IMAP, POP3, Z-Push,
web arayüzü, Outlook); kaynak koduna bağlanmaz. Yönetici API'si güvenlik
nedeniyle kısıtlı olduğu için ona bağımlılık en azda tutulur: yalnızca test
hazırlığı (hesap/kutu sıfırlama) için ve mümkünse ayrı bir test ortamında.

Ek ortam bilgisi: EMS'in kendine özel bir web istemcisi ve web API'si var;
takvim ve rehber kapsamda; hata takibi Jira; Outlook kurulu Windows test
makinesi POC sonrasında ayrılacak; yakın zamanda sunucu geçişi yok.

Kapsam genişleme sırası:

1. [x] **Koşu geçmişi, kanıt, sürümler arası fark** — diğer her şeyin temeli
2. [ ] **Posta kutusu işlemleri**: klasör, taşıma, silme, arama, kota; kanallar
       arası durum eşitlemesi (iPad'de okundu → Outlook'ta okundu)
3. [ ] **Web istemcisi testleri**: giriş, yazma, yanıtlama, ek, arama (tarayıcı otomasyonu)
4. [ ] **Outlook ajanı** (Windows): Outlook'un mesajı nasıl ayrıştırdığını nesne
       modeli üzerinden okur — kritik istemcide manuel yükü kaldırır
5. [ ] **Takvim ve rehber**: davet kabul/ret/güncelleme/iptal, ortak takvim, adres defteri
6. [ ] **Politika ve güvenlik paketi**: boyut sınırı, yasaklı uzantı, spam/virüs
       test dosyaları, hesap kilitleme, açık aktarım, otomatik yanıt/yönlendirme/kurallar
7. [ ] **Manuel test yönetimi**: yalnızca değişeni yeniden test et (fark raporundan),
       referans ekran görüntüsü, kişiye atama, KALDI → Jira kaydı, gereksinim izlenebilirliği

## Z-Push kanalını sahada doğrulamak

ActiveSync istemcisi yazıldı (okuma, görünüm kontrolleri, Z-Push üzerinden
gönderim) ama yalnızca sahte sunucuya karşı test edildi. Gerçek Z-Push'a karşı
`scripts/zpush_check.py` ile denenecek. Olası takılma noktaları: protokol
sürümü, cihaz ilkesi akışı, Z-Push'ın MIME ve HTML gövdeyi döndürme biçimi.

## 1. Bugün araç neyi kanıtlıyor, neyi kanıtlamıyor

| Katman | Durum | Açıklama |
| --- | --- | --- |
| Sunucu → sunucu taşıma (SMTP ile gönder, IMAP ile oku) | ✅ Otomatik | Mesajın MIME yapısı, başlıkları, ekleri sunucu çiftinden geçince bozuluyor mu |
| Bayt düzeyinde doğrulama | ✅ 15 katalog senaryosu | Gövde metni, ek SHA-256, başlık eşitliği — her koşuda aynı sonuç |
| LLM yorumu | ✅ 26 senaryonun tamamı | İlk 11 senaryoda **tek** karar kaynağı (tekrarlanabilir değil) |
| Alıcı **istemcinin** gösterimi (iOS Mail, Outlook…) | 👁 Yalnızca manuel | Manuel Doğrulama sihirbazı; otomasyon ekranı göremez |
| Gönderen **istemcinin** ürettiği MIME | ❌ | Mesajı Python kurar; iOS Mail / Gmail Android'in gerçek çıktısı test edilmez |

Dürüstçe söylenmesi gereken dört boşluk:

1. **"İstemci" bugün yalnızca bir etiket.** Checklist'teki 18 kombinasyon
   yalnızca **8 farklı sunucu yolu** içeriyor (ör. Gmail → EMS 6 kez). İstemci
   adı değişen kombinasyonlar otomasyon açısından aynı testi tekrar ediyor;
   fark yalnızca manuel adımda ortaya çıkıyor. Tam koşu 468 mesaj gönderir,
   bunların ~260'ı otomasyon için tekrardır.
2. ~~Kanıt saklanmıyor.~~ Giderildi: koşu geçmişi, ham mesaj kanıtı ve
   referans koşuyla fark eklendi (README → Koşu Geçmişi).
3. **Yalnızca INBOX'a bakılıyor.** Gereksiz (Junk) klasörüne düşen mesaj
   "teslim edilmedi — timeout" olarak raporlanıyor.
4. **Kimlik doğrulama yalnızca parola tabanlı.** Gmail ve Microsoft 365 temel
   kimlik doğrulamayı aşamalı olarak kapatıyor; OAuth2 (XOAUTH2) olmadan bu
   iki sunucu ürün kullanıcılarının çoğunda bağlanamayacak.

---

## 2. İstemci testleri: iOS'ta nasıl göründüğünü doğrulayabilir miyiz?

Kısa cevap: **bu araç tek başına hayır; kademeli olarak yaklaşabiliriz.**
IMAP ile okunan şey sunucudaki mesajdır, telefonun ekranı değil.

| Düzey | Yöntem | Ne kanıtlar | Maliyet |
| --- | --- | --- | --- |
| L0 | IMAP + bayt karşılaştırma (**var**) | Sunucu mesajı bozmadı | Yok |
| L1 | **İstemcinin protokolüyle** okuma (EAS / EWS / Graph / JMAP adaptörü) | Telefonun **aldığı** baytlar doğru. iOS Mail kurumsal sunucuya çoğunlukla ActiveSync ile bağlanır; sunucu mesajı EAS için yeniden biçimlendirir — IMAP'te sağlam görünen mesaj burada bozulabilir | Orta — en yüksek getirili otomasyon adımı |
| L2 | HTML'i WebKit'te iPhone görünümünde render edip ekran görüntüsü + görsel fark | "Yaklaşık böyle görünür" — iOS Mail'in kendisi değil, aynı motor ailesi. CSS destek matrisiyle statik uyarı eklenebilir | Düşük-orta |
| L3 | Gerçek cihaz otomasyonu (Appium) | Gerçek ekran | Yüksek: iOS Simülatöründe Mail uygulaması yok → **fiziksel iPhone + Mac** gerekir; her iOS sürümünde seçiciler kırılır. Android emülatörü ve web istemciler (OWA, EMS webmail → Playwright) çok daha ucuz |
| L4 | Manuel doğrulama sihirbazı (**var**) | İnsan gözü | Kişi-saat; uyum oranıyla seyreltilir |

Ticari servisler (Litmus, Email on Acid) gerçek istemci ekran görüntüsü verir
ama mesajı **kendi** posta kutularına alır: EMS yolunu test etmez ve kurum içi
içeriği dışarı çıkarır. Bu kullanım için uygun değil.

**Önerilen strateji:** L4 ile başla (bugün), her sürümde oto↔manuel uyum
oranını ölç; L1'i ekle; uyumu N sürüm boyunca %100 kalan senaryolarda manuel
adımı örneklemeye indir; L3'ü yalnızca en kritik 3-5 senaryo için düşün.

**Gönderen istemci için:** "altın örnek" yaklaşımı — her gerçek istemciden
(iOS Mail, Gmail Android, Outlook) senaryo başına bir kez elle gönderilmiş
mesajın ham hâli (`.eml`) depoya alınır; testte Message-ID/tarih yenilenip
SMTP'den yeniden oynatılır. Böylece "Gönderen İstemci" sütunu gerçek anlam
kazanır; istemci güncellenince örnek yenilenir ve farkı görülür.

---

## 3. Fazlar

### Faz 1 — Kanıtlanabilir regresyon çekirdeği

Hedef: iki koşu arasındaki farkı gösterebilmek.

- [ ] İlk 11 senaryoyu kataloğa taşı (beklenti + deterministik doğrulama);
      LLM her yerde danışman olsun
- [x] **Koşu geçmişi**: her koşu `reports/runs/<kimlik>/` altında kalıcı
- [x] **Kanıt paketi**: gönderilen ve alınan ham `.eml`, SHA-256 özetleri,
      manifest (araç sürümü, test planı özeti, sunucu EHLO yanıtı,
      IMAP `CAPABILITY`/`ID`, zaman damgaları)
- [x] **Sürüm etiketi**: koşuya "EMS 9.4.2", "iOS 19.1" gibi etiketler; raporda görünür
- [x] **Baseline ve fark raporu**: bir koşuyu referans işaretle; sonraki koşu
      için "yeni FAIL / düzelen / değişen [Bilgi] satırları (ör. sunucu artık
      8bit'i QP'ye çeviriyor) / başlık farkı" raporu
- [ ] İmzalanabilir rapor (PDF + özet dosyası) — denetim kanıtı
- [ ] Gereksiz klasörü taraması; teslim süresi ölçümü ve eşik (SLA)
- [ ] Kararsızlık yönetimi: geçici hatalarda yeniden deneme, "kararsız" etiketi
- [ ] Test mesajlarını koşu sonunda temizleme (posta kutusu şişmesin)
- [x] Kombinasyon tekilleştirme: test planı (yol · istemci · kademe · etki)
- [x] POP3 okuma kanalı (Outlook masaüstü yolu)

### Faz 2 — Kapsam ve istemci gerçekçiliği

- [ ] Negatif senaryolar: olmayan alıcı → NDR; boyut sınırı aşımı → 552;
      yasaklı uzantı / EICAR politikası; hatalı kimlikle reddedilme
- [ ] Kimlik doğrulama sonuçları: `Authentication-Results` (SPF/DKIM/DMARC), DKIM imzasının korunumu
- [ ] S/MIME şifreli mesaj, PGP/MIME; imza doğrulamasını araç içinde yapma
- [ ] Yanıt/iletme zincirlerinde gerçek istemci alıntı biçimleri; `format=flowed`
- [ ] Takvim: güncelleme / iptal (`METHOD:CANCEL`), yinelenen etkinlik, saat dilimi
- [ ] Klasör ve bayrak eşitlemesi (okundu, yıldızlı, taşıma), sunucu tarafı kurallar, otomatik yanıt
- [ ] Yük: art arda N mesaj, eşzamanlı gönderim, sıra korunumu
- [ ] "Altın örnek" `.eml` yeniden oynatma (gönderen istemci gerçekçiliği)
- [x] L1 protokol adaptörü: ActiveSync (Z-Push) — yazıldı, sahada doğrulanacak
- [ ] L2 WebKit render + görsel fark

### Faz 3 — Ürünleştirme (kur · yapılandır · çalıştır)

- [ ] **Genel sunucu modeli**: sabit `ems/gmail/outlook` yerine adlandırılmış
      N uç nokta; kombinasyon matrisi ve test planı arayüzden (CSV yerine)
- [ ] **OAuth2 / XOAUTH2** (Gmail, Microsoft 365)
- [ ] **Sır yönetimi**: `config.yaml`'daki düz metin parolalar → ortam
      değişkeni / işletim sistemi anahtarlığı / şifreli kasa
- [ ] İlk çalıştırma sihirbazı: uç nokta ekle → bağlantı testi → örnek koşu
- [ ] Paketleme: sürümlü Docker imajı, `pipx` kurulumu, `--version`, CHANGELOG, sürüm notları
- [ ] Zamanlanmış koşular + bildirim (e-posta / webhook); CI için JUnit XML çıktısı
- [ ] Çok kullanıcı: roller, manuel onayda "kim, ne zaman" denetim izi
- [ ] Kapalı ağ: LLM'siz kip (**var**), yerel LLM seçeneği, arayüzde dış
      kaynak (web fontu) bağımlılığını kaldırma
- [ ] Arayüz/kod bakımı: tek dosyalık `app.py` ve `index.html`'in bölünmesi;
      uygulama içi kılavuzun güncellenmesi (hâlâ "5 senaryo" yazıyor); TR/EN arayüz

---

## 4. Sürüm güncellemesinde önerilen regresyon protokolü

1. Güncellemeden **önce** tam plan koşusu → "baseline" olarak işaretle (sürüm etiketiyle).
2. Güncellemeyi uygula.
3. Aynı planı koş → fark raporu.
4. Fark yoksa: manuel doğrulamayı örnekle (kritik senaryolar + rastgele %10).
   Fark varsa: farklı satırların tamamını manuel doğrula.
5. Kanıt paketini (rapor + ham mesajlar + manifest) sürüm kaydına ekle.

Faz 1 tamamlandığında bu protokolün 1., 3. ve 5. adımları tek komut olur.

---

## Açık kararlar

2. Outlook masaüstü otomasyonu (Windows üzerinde arayüz otomasyonu) istenir mi, yoksa POP3 kanalı + manuel doğrulama yeterli mi?
3. Outlook/O365 ve Gmail için OAuth2 gerekli mi (hesaplarda uygulama parolası kullanılabiliyor mu)?
