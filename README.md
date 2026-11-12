# 📧 Mail Servis Otomasyon Sistemi

Claude API destekli, SMTP/IMAP tabanlı mail iletim testi.  
Web arayüzü (Flask), 2FA/TOTP desteği ve otomatik MIME analizi içerir.

---

## 🏗️ Proje Yapısı

```text
mail_automation/
├── app.py                # Flask web arayüzü — buradan başlat
├── main.py               # CLI orkestratör (arayüz veya doğrudan çalıştırılabilir)
├── sender.py             # SMTP gönderim (11 senaryo tipi)
├── receiver.py           # IMAP alım & MIME ayrıştırma
├── mail_transport.py     # SMTP/IMAP bağlantı + TLS ayarları (iç CA, 465 SSL)
├── analyzer.py           # Claude API analiz motoru
├── auth_manager.py       # 2FA / TOTP akış yöneticisi
├── csv_parser.py         # Test checklist CSV okuyucu
├── reporter.py           # HTML + CSV rapor üretici
├── config.yaml           # ⚠️ Sunucu & API ayarları (.gitignore'da, commit etme!)
├── config.yaml.example   # Güvenli şablon — bunu kopyala
├── requirements.txt
├── .gitignore
├── github_setup.sh       # GitHub repo kurulum scripti
├── scripts/
│   ├── e2e_smoke.py      # Gerçek sunucuya duman testi
│   └── extend_checklist.py  # Checklist CSV'sini senaryolarla genişletir
├── templates/
│   └── index.html        # Web arayüzü
├── test_files/           # Otomatik oluşturulur
├── reports/              # HTML ve CSV raporlar
└── logs/                 # Log dosyaları
```

---

## ⚙️ Kurulum

```bash
# 1. (Önerilen) İzole sanal ortam oluştur
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip

# 2. Bağımlılıkları kur
pip install -r requirements.txt

# 3. Config dosyasını oluştur
cp config.yaml.example config.yaml

# 4. Web arayüzünü başlat
python app.py
# → http://localhost:5000 (boşsa)
# → 5000 doluysa otomatik: http://localhost:5001 (veya bir sonraki boş port)
```

Config bilgilerini (SMTP/IMAP, API key, 2FA) doğrudan web arayüzünden girebilirsin.

---

## 🖥️ Web Arayüzü

`python app.py` ile başlatılır, `http://localhost:5000` adresinde açılır.  
Eğer `5000` doluysa uygulama otomatik olarak bir sonraki boş porta geçer (ör. `5001`).

| Sekme | Açıklama |
| --- | --- |
| **Dashboard** | Test matrisi özeti ve son sonuçlar |
| **Konfigürasyon** | Sunucu SMTP/IMAP bilgileri, Claude API key, test parametreleri |
| **Güvenlik & 2FA** | Her sunucu için kimlik doğrulama yöntemi ve TOTP ayarları |
| **Test Çalıştır** | Kombinasyon ve senaryo seçimi, normal/dry-run modu |
| **Canlı Loglar** | SSE ile gerçek zamanlı terminal çıktısı |
| **Sonuçlar** | PASS/FAIL tablosu, güven seviyesi, özet |
| **Raporlar** | HTML raporu görüntüle, CSV indir |

İstersen portu sabitlemek için:

```bash
PORT=5050 python app.py
```

Diğer ortam değişkenleri:

| Değişken | Varsayılan | Açıklama |
|----------|------------|----------|
| `PORT` | `5000` (doluysa sıradaki boş port) | Sunucu portu |
| `HOST` | `127.0.0.1` | Ağdan erişim için `HOST=0.0.0.0` ayarla |
| `FLASK_DEBUG` | kapalı | `FLASK_DEBUG=1` ile debug/reloader açılır (yalnızca geliştirme için — debugger uzaktan kod çalıştırmaya izin verir) |
| `UI_PASSWORD` | boş | Ayarlanırsa tüm arayüz HTTP Basic Auth ister. **Boşsa arayüz yalnızca bu makineden (localhost) kullanılabilir**; ağdan gelen istekler 403 alır |
| `UI_USERNAME` | `admin` | Basic Auth kullanıcı adı (`UI_PASSWORD` ile birlikte) |
| `ALLOW_NO_AUTH` | kapalı | `1` ise parolasız arayüze ağdan erişime bilerek izin verir (önerilmez — kayıtlı şifreler açığa çıkar) |
| `DATA_DIR` | repo kökü | `config.yaml`, `reports/`, `logs/` dizini (konteynerde kalıcı volume) |

```bash
# Ağa açık + parola korumalı çalıştırma örneği
HOST=0.0.0.0 UI_PASSWORD='guclu-parola' python app.py
```

> **Güvenlik notu:** `UI_PASSWORD` yoksa arayüz yalnızca `localhost`'tan
> kullanılabilir (uzak istekler, yabancı `Host` başlıkları ve ters proxy
> başlığı taşıyan istekler 403 alır — DNS rebinding ve `proxy_pass`'e karşı).
> Ağa/internete açarken `UI_PASSWORD` zorunludur. Host'u yeniden yazan bir ters
> proxy (nginx) kullanıyorsanız `X-Forwarded-Host` başlığını iletin.
> Başka sitelerden gelen durum değiştiren istekler (CSRF) reddedilir,
> `/api/config` yanıtlarında şifreler ve API key maskelenir, bağlantı testi
> kayıtlı şifreyi yalnızca kayıtlı sunucu+kullanıcı için kullanır.

---

## 🔐 Güvenlik & 2FA

Her sunucu için 4 kimlik doğrulama yöntemi desteklenir:

| Yöntem | Açıklama | Kullanım |
| --- | --- | --- |
| `password` | Standart kullanıcı adı + şifre | 2FA kapalı hesaplar |
| `app_password` | Uygulama özel şifresi | Gmail App Password, Microsoft App Password |
| `totp_password` | Şifre + TOTP/OTP kodu | 2FA zorunlu EMS/kurumsal sunucular |
| `otp_only` | Yalnızca tek kullanımlık kod | SMS veya authenticator tabanlı giriş |

**TOTP Otomatik Üretim:** Authenticator uygulamanızdaki Base32 secret'ı kaydederseniz, her test çalışmasında kod otomatik üretilir — sizi durdurmaz. Secret kaydetmek istemiyorsanız boş bırakın; web arayüzünden başlatılan testlerde ekrana modal açılır ve kodu girersiniz (test süreci ile arayüz, `logs/mfa_bridge/` altındaki dosya köprüsü üzerinden haberleşir). Girilen kod 20 saniye boyunca aynı sunucunun yeniden bağlanmalarında tekrar sorulmadan kullanılır.

> Doğrudan CLI'dan (`python main.py`) çalıştırmada modal akışı devre dışıdır —
> secret kayıtlı değilse yalnızca şifre ile giriş denenir. Modal akışını CLI'da
> zorlamak isterseniz `MFA_INTERACTIVE=1 python main.py` ile başlatıp kodu başka
> bir terminalden web arayüzüne girebilirsiniz.

**OTP tipleri:** TOTP / SMS / E-posta OTP / Push bildirimi

---

## 📊 Test Senaryoları

Her kombinasyon için **11 senaryo** çalıştırılır (18 kombinasyon × 11 senaryo ×
5 kontrol noktası = 990 adım):

| Senaryo | MIME Yapısı | Ne Test Eder |
| --- | --- | --- |
| `plain_text` | `text/plain` | UTF-8 encoding, header bütünlüğü, Türkçe karakter (ğüşıöç) |
| `attachment` | `multipart/mixed` | MIME type, dosya adı/boyutu, base64 encoding |
| `multi_attachment` | `multipart/mixed` | Çoklu ek: adet, ad/boyut korunumu, karma formatlar |
| `inline_image` | `multipart/related` | CID referansı, HTML yapısı, resim bütünlüğü |
| `reply_chain` | `multipart/alternative` | In-Reply-To/References, alıntı yapısı, thread zinciri |
| `forward` | `multipart/mixed` + `message/rfc822` | İletme kapsüllemesi, orijinal header'ların korunumu, boundary izolasyonu |
| `html_table` | `multipart/alternative` | Tablo yapısı (tr/td/th), hücre stilleri, düz metin yedeği |
| `complex_html` | `multipart/alternative` | Zengin CSS, `@media` sorguları, duyarlı yerleşim, sanitizasyon |
| `i18n` | `text/plain` | RFC 2047 başlık kodlaması, Arapça/CJK/emoji, çok alfabeli gövde |
| `calendar_invite` | `text/calendar` + `application/ics` | iTIP `METHOD=REQUEST`, VEVENT bütünlüğü, `invite.ics` eki |
| `smime` | `multipart/signed` | Dijital imza (sertifika yoksa otomatik atlanır) |

**Senaryo alt kümesi çalıştırma** — tam koşu 990 adım olduğu için genelde
filtreleyerek çalışmak istersiniz:

```bash
python main.py --scenario calendar_invite       # yalnızca takvim daveti
python main.py --combo 0 --scenario i18n        # tek kombinasyon + tek senaryo
```

> **S/MIME:** Altyapıda dijital imza sertifikası tanımlı değilse otomatik olarak
> atlanır (`config.yaml` → `test.smime_cert_path` / `test.smime_key_path`).
> Kurumsal metin imzası (e-posta altı imza) tüm senaryolarda gövde içeriği
> üzerinden taşınır.

### Senaryoya özel ayarlar

```yaml
test:
  # Çoklu ek senaryosu — tanımlanmazsa prepare_test_files() üretir
  test_multi_attachment_paths:
    - "test_files/test_document.pdf"
    - "test_files/test_data.csv"
    - "test_files/test_notes.txt"

  # Takvim daveti
  calendar_duration_minutes: 30
  calendar_location: "Çevrimiçi Toplantı"
```

### Checklist CSV'sini genişletme

Yeni senaryolar `mail_test_checklist.csv`'ye betikle eklenir; betik
idempotenttir (var olan blokları tekrarlamaz) ve adım numaralarını yeniden verir:

```bash
python scripts/extend_checklist.py --dry-run    # ne ekleneceğini göster
python scripts/extend_checklist.py              # uygula (.bak yedeği alır)
```

---

## 🧠 Claude API Analiz Akışı

```text
CSV'den Senaryo Oku
       ↓
  Sender (SMTP)
  → plain_text / attachment / multi_attachment / inline_image
  → reply_chain / forward / html_table / complex_html
  → i18n / calendar_invite / smime
       ↓
  Receiver (IMAP polling)
  → ham MIME ayrıştırma
  → headers, parts, attachments, inline_images
       ↓
  Analyzer (Claude API)
  → senaryo tipine özel prompt
  → kontrol noktaları değerlendirmesi
  → JSON: {passed, confidence, checks, issues, recommendations}
       ↓
  Reporter
  → reports/test_report.html  (görsel)
  → reports/test_results.csv  (Sheets uyumlu)
```

---

## 🚀 CLI Kullanımı (arayüz olmadan)

```bash
# Tüm testleri çalıştır
python main.py

# Sadece 0. kombinasyonu çalıştır
python main.py --combo 0

# Sadece plain_text senaryosu
python main.py --scenario plain_text

# Bağlantı testi (mail göndermez)
python main.py --dry-run

# Birden fazla kombinasyon
python main.py --combo 2,5,7

# Belirli kombinasyon + senaryo
python main.py --combo 2 --scenario attachment

# Farklı CSV dosyası
python main.py --csv /yol/baska_checklist.csv
```

---

## 🔧 config.yaml Alanları

```yaml
ems:
  smtp_host: "ems.sirket.local"
  smtp_port: 587
  smtp_use_tls: true
  imap_host: "ems.sirket.local"
  imap_port: 993
  imap_use_ssl: true
  username: "test@sirket.local"
  password: ""                    # Web arayüzünden gir
  test_address: "test@sirket.local"
  label: "EMS On-Prem"
  auth_method: "totp_password"    # password|app_password|totp_password|otp_only
  mfa_method: "totp"              # totp|sms|email_otp|push
  totp_secret: ""                 # Opsiyonel — boş bırakırsan her çalışmada sorar
  # TLS — kurum içi sunucular için (hepsi opsiyonel)
  smtp_use_ssl: false             # 465 doğrudan SSL; port 465 ise otomatik açılır
  imap_use_starttls: false        # 143 üzerinde STARTTLS
  tls_ca_file: ""                 # Self-signed / iç CA: kurum CA sertifikası (PEM)
  tls_verify: true                # false = doğrulama kapalı (güvensiz, yalnızca test)

anthropic:
  api_key: ""                     # https://console.anthropic.com → API Keys
  model: "claude-opus-5-5"        # boş bırakılırsa varsayılan (claude-opus-5-5)
  effort: "medium"                # low | medium | high — analiz derinliği/maliyet

analysis:
  provider: "claude"              # claude | gemini — analiz için kullanılacak LLM

gemini:                           # yalnızca provider: gemini iken gerekli
  api_key: ""                     # https://aistudio.google.com/apikey
  model: "gemini-2.0-flash"

test:
  wait_seconds: 15                # Mesajın IMAP'te görünmesini bekleme süresi
  max_retries: 3
  retry_interval: 5
  subject_prefix: "[AUTO-TEST]"
```

> `config.yaml` `.gitignore`'da tanımlıdır — şifreler ve secret'lar repoya gitmez.  
> Repoya yalnızca `config.yaml.example` commit edilir.

### Kurum içi (EMS) sunucunun sertifikası

Kurum içi mail sunucuları çoğunlukla kendinden imzalı ya da kurumun kendi CA'sı
ile imzalanmış sertifika kullanır; Python bunu varsayılan olarak reddeder
(`certificate verify failed`). İki seçenek:

1. **Önerilen:** Kurumun kök CA sertifikasını (PEM, BT ekibinden alınır) veri
   dizinine koyun ve `tls_ca_file: "certs/kurum-ca.pem"` yazın. Doğrulama açık kalır.
2. **Yalnızca test ortamı:** `tls_verify: false` — bağlantı şifreli ama sunucu
   kimliği doğrulanmaz (araya girme saldırısına açık).

Her ikisi de web arayüzünde EMS kartından ayarlanabilir; **🔌 Bağlantı Test Et**
artık SMTP'nin yanında IMAP'i de sınar ve hata olursa sebebini (sertifika, port/TLS
modu uyuşmazlığı, DNS, kimlik doğrulama) Türkçe açıklar.

### Analiz modeli ve maliyet

Varsayılan model **Claude Opus 5.5** (`claude-opus-5-5`). Her senaryo bir API
çağrısıdır; tam matris (18 kombinasyon × 11 senaryo) ~200 çağrı yapar. Yanıtlar
JSON şemasıyla (yapılandırılmış çıktı) alınır ve doğrulanır; geçersiz/eksik
karar asla PASS sayılmaz. Daha ekonomik koşu için arayüzden Sonnet 5.5 ya da
Haiku 5.5 seçilebilir veya `effort: low` kullanılabilir.

API'ye art arda 3 kez ulaşılamazsa (ağ, zaman aşımı, 5xx) ya da API key/model
geçersizse kalan senaryolar LLM'e gönderilmez ve "analiz yapılmadı" FAIL'i
olarak raporlanır — ölü bir uç nokta koşuyu saatlerce kilitlemez.

---

## 📋 Raporlar

Her test çalışmasının ardından iki dosya üretilir:

- `reports/test_report.html` — Kombinasyon bazlı görsel rapor (web arayüzünden görüntülenebilir)
- `reports/test_results.csv` — Google Sheets'e aktarılabilir sonuçlar

Yarıda kalan her senaryo **FAIL** satırı olarak raporlanır (sessizce düşmez);
sunucusu `config.yaml`'da tanımlı olmayan ya da eksik kombinasyonların
senaryoları da "Konfigürasyon" FAIL'i olarak rapora girer.
Koşu durdurulursa (arayüzde **Durdur** / Ctrl-C) o ana kadarki sonuçlarla kısmi
rapor yazılır. `main.py` çıkış kodları (CI/zamanlanmış koşular için):

| Kod | Anlamı |
| --- | --- |
| `0` | Tüm (atlanmayan) senaryolar PASS |
| `2` | Koşu tamamlandı, en az bir senaryo FAIL (dry-run'da: bağlantı hatası) |
| `130` | Koşu durduruldu — kısmi rapor yazıldı |
| `1` | Araç/config/CSV hatası, hiç sonuç üretilemedi ya da rapor dosyası yazılamadı |

---

## 🌐 Web'de Yayınlama (Deploy)

Uygulama arka planda uzun süren test süreçleri çalıştırdığı için **kalıcı
konteyner** sunan platformlar tam işlevlidir; serverless platformlarda yalnızca
arayüz/konfigürasyon çalışır:

| Platform | Test Koşusu | Giden SMTP | Not |
| --- | --- | --- | --- |
| **Railway** | ✅ | ⚠️ **yalnızca Pro plan** | Free/Trial/Hobby'de SMTP kapalı. Pro'ya geçince yeniden deploy edin. `railway.toml` hazır; volume `/data` + `DATA_DIR=/data` |
| **Fly.io** | ✅ | ✅ (hesap bazlı engel yok*) | `fly.toml` hazır (volume + healthcheck) |
| **VPS** (Hetzner, DigitalOcean …) | ✅ | ⚠️ sağlayıcıya bağlı | Yeni hesaplarda 465/587 kapalı olabilir; destekten açtırılır* |
| **Kurum içi sunucu / Docker** | ✅ | ✅ (ağınıza bağlı) | EMS'e doğrudan erişim için en sorunsuz seçenek; `docker compose up -d` |
| **Render** | ✅ arayüz | ❌ **engelli** | Ücretsiz/paylaşımlı planlarda giden SMTP portları kapalı — mail **gönderemez** |
| **Vercel** | ❌ (60 sn serverless) | ❌ | Yalnızca arayüz demosu |

\* Politikalar değişebilir; topluluk/forum bilgisine dayanır. Deploy sonrası
mutlaka `/api/diagnostics/network` ile doğrulayın.

> **EMS on-prem notu:** Bulut platformundan kurum içi EMS sunucusuna erişmek için
> EMS'in SMTP/IMAP portlarının internete açık (ya da VPN üzerinden erişilebilir)
> olması gerekir. Kurum ağı dışa kapalıysa uygulamayı kurum içindeki bir
> makinede (Docker) çalıştırın.

> ⚠️ **En kritik nokta — giden SMTP:** Bu araç gerçek mail gönderdiği için
> sunucunun 587/465 portlarına **çıkışına izin veren** bir platform şarttır.
> **Render, Heroku ve çoğu ücretsiz PaaS spam'i önlemek için giden SMTP'yi
> engeller** → test başlatınca `Network is unreachable` (Errno 101) alırsınız.
> Bu bir kod hatası değil, platform politikasıdır.
>
> Deploy ettikten sonra tarayıcıdan **`/api/diagnostics/network`** adresini açın
> (ör. `https://siteniz.com/api/diagnostics/network`) — hangi mail portlarının
> açık olduğunu ve mail gönderilip gönderilemeyeceğini tek bakışta gösterir.
>
> **Mail göndermek için:** Railway Pro, Fly.io veya portu açık bir VPS kullanın;
> ya da uygulamayı SMTP'si açık bir ağda çalıştırın. Teşhis sayfası config'e
> kayıtlı sunucuların (ör. EMS) portlarını da dener ve DNS / zaman aşımı /
> reddedilme ayrımını gösterir.

Tüm platformlarda:

1. Repoyu GitHub'dan bağlayın — yapılandırma dosyaları (`railway.toml`,
   `fly.toml`, `render.yaml`, `vercel.json`, `Dockerfile`) hazır.
2. **`UI_PASSWORD` env değişkeni zorunlu** — ayarlanmazsa arayüz ağdan
   gelen her isteğe 403 döner (yalnızca localhost'ta parolasız çalışır).
3. PaaS ortamları otomatik algılanır ve `0.0.0.0`'a bind edilir; yerelde
   varsayılan `127.0.0.1` olarak kalır.
4. Healthcheck: `GET /api/health` (kimlik doğrulamasız, platform sağlık
   kontrolleri için).

```bash
# Docker ile (parola .env dosyasından okunur)
echo "UI_PASSWORD=guclu-parola" > .env
docker compose up -d      # → http://localhost:5005

# Fly.io ile
fly launch --copy-config --no-deploy
fly volumes create mail_data --size 1
fly secrets set UI_PASSWORD='guclu-parola'
fly deploy
```

> Deploy edilen sunucunun SMTP (587/465) ve IMAP (993) portlarına **dışarı
> çıkışına** izin verdiğinden emin olun; bazı ücretsiz planlar mail portlarını
> kapatır.

---

## 🚢 GitHub'a Yükleme

```bash
# gh CLI gerekmektedir: brew install gh
gh auth login
bash github_setup.sh
```

Script; `.gitignore`'u uygular, ilk commit'i oluşturur ve `mail-test-otomasyon` adıyla public repo açar.

---

## 🧪 Testler

Test paketi gerçek SMTP/IMAP/Claude bağlantısı olmadan (mock ile) çalışır:

```bash
pip install -r requirements.txt -r requirements-test.txt
python -m pytest

# Kapsam raporu ile
python -m pytest --cov=. --cov-report=term-missing
```

696 test, **%97 satır kapsamı** (çekirdek modüller %92-100). GitHub Actions üzerinde her push/PR
için Python 3.10/3.11/3.12 matrisinde otomatik çalışır
(`.github/workflows/tests.yml`).

| Dosya | Kapsam |
| --- | --- |
| `test_smoke.py` | Modül birim testleri — sender/receiver/analyzer/parser/reporter/TOTP |
| `test_smoke_api.py` | Flask uç noktaları — config, MFA, kombinasyon, çalıştırma, rapor |
| `test_e2e.py` | 18 kombinasyon × senaryo pipeline'ı (mock SMTP/IMAP/LLM) |
| `test_improvements.py` | Secret maskeleme, Basic Auth, path traversal, deploy, Gemini |
| `test_orchestrator.py` | `main.py`: senaryo yönlendirme, subject üretimi, atlama/hata yolları, CLI |
| `test_app_endpoints.py` | Config içe/dışa aktarma, log polling, alt süreç çalıştırıcı, 2FA bağlantı testi |
| `test_transport.py` | SMTP/IMAP giriş geri düşüşleri, IMAP yeniden deneme, S/MIME imzalama |
| `test_analyzer_errors.py` | Claude/Gemini API hata yolları (401, 429, 5xx, timeout, refusal) |
| `test_ui.py` | **Tarayıcı testleri** — gezinme, secret gizliliği, form, log akışı, 2FA modal |
| `test_new_scenarios.py` | Takvim daveti (ICS/iTIP), i18n, zengin HTML, forward, çoklu ek |

> S/MIME imzalama testleri sistemde `openssl` CLI yoksa otomatik atlanır.

### Arayüz (tarayıcı) testleri

`templates/index.html` ~600 satır inline JS içerir; pytest bunu çalıştıramadığı
için arayüz gerçek bir tarayıcıda test edilir. Playwright kurulu değilse
`test_ui.py` otomatik atlanır — temel paket ek bağımlılık istemez.

```bash
pip install playwright
playwright install chromium
python -m pytest tests/test_ui.py        # 29 test, ~30 sn
```

Kapsanan kritik davranışlar:

- Kayıtlı şifre/API key'in tarayıcıya **düz metin gitmemesi** (maskeleme)
- `localStorage`'a **hiçbir secret yazılmaması**
- Şifreye dokunmadan Kaydet'e basıldığında **kayıtlı şifrenin silinmemesi**
- Sayfa yüklenirken JS hatası olmaması, tüm sekmelerin açılması
- Log polling'in çıktıyı UTF-8 bozmadan basması ve bitişte durması
- Sunucu 2FA istediğinde modal'ın açılması

### Gerçek sunucu duman testi

Mock'suz, gerçek bir SMTP hesabından gerçek bir alıcıya 4 senaryoyu gönderir ve
gönderen kutuda bounce kontrolü yapar (alıcı taraf görsel olarak doğrulanır):

```bash
cp config.yaml.example config.yaml   # gönderen hesabı doldur (örn. gmail + app password)
python scripts/e2e_smoke.py --to alici@ornek.com
python scripts/e2e_smoke.py --to alici@ornek.com --scenarios plain_text --dry-run
```

---

## 🔍 Sorun Giderme

| Hata | Çözüm |
| --- | --- |
| SMTP auth hatası | `auth_method` ayarını kontrol et; Gmail için App Password kullan. Kimlik hatasında o sunucunun kalan senaryoları hesap kilitlenmesin diye denenmez |
| `certificate verify failed` | Kurum içi sertifika: `tls_ca_file` ile kurum CA'sını verin (bkz. *Kurum içi sunucunun sertifikası*) |
| `wrong version number` / 465'te bekleme | TLS modu–port uyuşmazlığı: 465 → `smtp_use_ssl: true`, 587 → STARTTLS |
| IMAP timeout | `wait_seconds` değerini artır (varsayılan: 15) |
| "IMAP kontrol edilemedi" | Mesaj gitmiş olabilir ama alıcı kutusuna bağlanılamadı — IMAP host/port/SSL/şifre |
| Mesaj bulunamıyor | Eşleştirme Message-ID ile yapılır; sunucu mesajı spam/başka klasöre almış olabilir |
| Arayüz 403 "yalnızca localhost" | `UI_PASSWORD` ayarlayın (ağdan/konteynerden erişim) |
| Claude API 401 | API key'i Konfigürasyon sayfasından güncelle |
| 2FA modal açılmıyor | Tarayıcı popup engelleyicisini kapat |
| TOTP kodu hatalı | `totp_secret` Base32 formatında olmalı, boşluk içermemeli |
| Flask bulunamadı | `pip install -r requirements.txt` çalıştır |
