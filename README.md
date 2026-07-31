# 📧 Mail Servis Otomasyon Sistemi

Claude API destekli, SMTP/IMAP tabanlı mail iletim testi.  
Web arayüzü (Flask), 2FA/TOTP desteği ve otomatik MIME analizi içerir.

---

## 🏗️ Proje Yapısı

```text
mail_automation/
├── app.py                # Flask web arayüzü — buradan başlat
├── main.py               # CLI orkestratör (arayüz veya doğrudan çalıştırılabilir)
├── sender.py             # SMTP gönderim (4 senaryo tipi)
├── receiver.py           # IMAP alım & MIME ayrıştırma
├── analyzer.py           # Claude API analiz motoru
├── auth_manager.py       # 2FA / TOTP akış yöneticisi
├── csv_parser.py         # Test checklist CSV okuyucu
├── reporter.py           # HTML + CSV rapor üretici
├── config.yaml           # ⚠️ Sunucu & API ayarları (.gitignore'da, commit etme!)
├── config.yaml.example   # Güvenli şablon — bunu kopyala
├── requirements.txt
├── .gitignore
├── github_setup.sh       # GitHub repo kurulum scripti
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
| `UI_PASSWORD` | boş (auth kapalı) | Ayarlanırsa tüm arayüz HTTP Basic Auth ister |
| `UI_USERNAME` | `admin` | Basic Auth kullanıcı adı (`UI_PASSWORD` ile birlikte) |

```bash
# Ağa açık + parola korumalı çalıştırma örneği
HOST=0.0.0.0 UI_PASSWORD='guclu-parola' python app.py
```

> **Güvenlik notu:** Varsayılan olarak arayüz yalnızca `localhost`'tan erişilebilir
> ve kimlik doğrulama kapalıdır. `HOST=0.0.0.0` ile ağa açacaksanız mutlaka
> `UI_PASSWORD` ayarlayın. `/api/config` yanıtlarında şifreler ve API key maskelenir.

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

| Senaryo | Ne Test Eder |
| --- | --- |
| `plain_text` | UTF-8 encoding, header bütünlüğü, Türkçe karakter (ğüşıöç) |
| `attachment` | MIME type, dosya adı/boyutu, base64 encoding |
| `inline_image` | CID referansı, HTML yapısı, resim bütünlüğü |
| `reply_chain` | In-Reply-To/References headers, alıntı yapısı, thread zinciri |

> **Not:** S/MIME senaryosu bu projede aktif değildir. Altyapıda dijital imza özelliği
> bulunmadığından otomatik olarak atlanır. Kurumsal metin imzası (e-posta altı imza)
> `plain_text` ve `reply_chain` senaryolarında gövde içeriği üzerinden test edilir.

---

## 🧠 Claude API Analiz Akışı

```text
CSV'den Senaryo Oku
       ↓
  Sender (SMTP)
  → plain_text / attachment / inline_image / reply_chain
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

anthropic:
  api_key: ""                     # https://console.anthropic.com → API Keys
  model: "claude-opus-4-8"        # boş bırakılırsa varsayılan (claude-opus-4-8) kullanılır

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

---

## 📋 Raporlar

Her test çalışmasının ardından iki dosya üretilir:

- `reports/test_report.html` — Kombinasyon bazlı görsel rapor (web arayüzünden görüntülenebilir)
- `reports/test_results.csv` — Google Sheets'e aktarılabilir sonuçlar

---

## 🌐 Web'de Yayınlama (Deploy)

Uygulama arka planda uzun süren test süreçleri çalıştırdığı için **kalıcı
konteyner** sunan platformlar tam işlevlidir; serverless platformlarda yalnızca
arayüz/konfigürasyon çalışır:

| Platform | Test Koşusu | Giden SMTP | Not |
| --- | --- | --- | --- |
| **Railway** (önerilen) | ✅ | ✅ açık | `railway.toml` hazır; volume için `/data` + `DATA_DIR=/data` |
| **Fly.io / VPS** | ✅ | ✅ açık | Mail göndermeye tam uygun |
| **Docker (kendi sunucun)** | ✅ | ✅ (sunucuna bağlı) | `docker compose up -d` yeterli |
| **Render** | ✅ arayüz | ❌ **engelli** | Ücretsiz/paylaşımlı planlarda giden SMTP portları kapalı — mail **gönderemez** |
| **Vercel** | ❌ (60 sn serverless) | ❌ | Yalnızca arayüz demosu |

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
> **Mail göndermek için:** Railway, Fly.io veya bir VPS kullanın; ya da
> uygulamayı SMTP'si açık bir ağda çalıştırın.

Tüm platformlarda:

1. Repoyu GitHub'dan bağlayın — yapılandırma dosyaları (`railway.toml`,
   `render.yaml`, `vercel.json`, `Dockerfile`) hazır.
2. **`UI_PASSWORD` env değişkenini mutlaka ayarlayın** — internete açık
   arayüz parolasız kalmasın.
3. PaaS ortamları otomatik algılanır ve `0.0.0.0`'a bind edilir; yerelde
   varsayılan `127.0.0.1` olarak kalır.
4. Healthcheck: `GET /api/health` (kimlik doğrulamasız, platform sağlık
   kontrolleri için).

```bash
# Docker ile
docker compose up -d      # → http://localhost:5005
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

498 test, ~6 saniye, **%99 satır kapsamı**. GitHub Actions üzerinde her push/PR
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

> S/MIME imzalama testleri sistemde `openssl` CLI yoksa otomatik atlanır.

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
| SMTP auth hatası | `auth_method` ayarını kontrol et; Gmail için App Password kullan |
| IMAP timeout | `wait_seconds` değerini artır (varsayılan: 15) |
| Mesaj bulunamıyor | `subject_prefix` sunucu filtresine takılıyor olabilir |
| Claude API 401 | API key'i Konfigürasyon sayfasından güncelle |
| 2FA modal açılmıyor | Tarayıcı popup engelleyicisini kapat |
| TOTP kodu hatalı | `totp_secret` Base32 formatında olmalı, boşluk içermemeli |
| Flask bulunamadı | `pip install -r requirements.txt` çalıştır |
