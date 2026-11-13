# Ürün Yol Haritası — Mail Regresyon Test Aracı

Amaç: bu projeyi, bir kurumun **kurup yapılandırıp çalıştırabileceği** ve
mesajlaşma ürünü ya da istemci güncellemelerinde "mesaj alma/gönderme
bozulmadı" iddiasını **kanıtlayabileceği** bir ürüne dönüştürmek.

Bu belge bir tartışma taslağıdır; sonundaki [açık kararlar](#açık-kararlar)
netleştikçe fazların sırası değişebilir.

---

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
2. **Kanıt saklanmıyor.** Gönderilen/alınan ham mesajlar atılıyor, her koşu
   bir öncekinin raporunun üzerine yazıyor, raporda sunucu/araç sürümü yok.
   "Güncellemeden önce de böyleydi" bugün gösterilemez.
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
- [ ] **Koşu geçmişi**: her koşu `runs/<kimlik>/` altında kalıcı (SQLite dizini)
- [ ] **Kanıt paketi**: gönderilen ve alınan ham `.eml`, SHA-256 özetleri,
      manifest (araç sürümü, test planı özeti, sunucu EHLO yanıtı,
      IMAP `CAPABILITY`/`ID`, zaman damgaları)
- [ ] **Sürüm etiketi**: koşuya "EMS 9.4.2", "iOS 19.1" gibi etiketler; raporda görünür
- [ ] **Baseline ve fark raporu**: bir koşuyu referans işaretle; sonraki koşu
      için "yeni FAIL / düzelen / değişen [Bilgi] satırları (ör. sunucu artık
      8bit'i QP'ye çeviriyor) / başlık farkı" raporu
- [ ] İmzalanabilir rapor (PDF + özet dosyası) — denetim kanıtı
- [ ] Gereksiz klasörü taraması; teslim süresi ölçümü ve eşik (SLA)
- [ ] Kararsızlık yönetimi: geçici hatalarda yeniden deneme, "kararsız" etiketi
- [ ] Test mesajlarını koşu sonunda temizleme (posta kutusu şişmesin)
- [ ] Kombinasyon tekilleştirme: aynı sunucu yolu otomasyonda bir kez koşsun,
      istemci çeşitlemesi yalnızca manuel/istemci katmanında çoğalsın

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
- [ ] L1 protokol adaptörü (EAS/EWS/Graph — hangisi olduğu açık karar)
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

1. iOS / Android istemciler EMS'e **hangi protokolle** bağlanıyor (ActiveSync, IMAP, özel uygulama)? L1 adaptörünün ne olacağını bu belirler.
2. Elde test cihazı / Mac var mı? (L3'ün mümkün olup olmadığı)
3. Mesaj içeriğinin dış LLM API'sine gitmesi kabul edilebilir mi, yoksa varsayılan LLM'siz kip mi olmalı?
4. Hedef kitle: yalnızca kurum içi mi, başka kurumlara dağıtılacak ürün mü? (çok kullanıcı, lisans, EN arayüz önceliği)
5. Checklist'teki 18 kombinasyon korunmalı mı, yoksa "sunucu yolu × istemci" olarak yeniden mi modellenmeli?
