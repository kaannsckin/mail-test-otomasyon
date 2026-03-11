"""
message_templates.py — Senaryo bazlı e-posta taslak mesajları.

Her senaryo tipi için kısa / orta / uzun olmak üzere 3 farklı uzunlukta
gerçekçi Türkçe iş yazışması şablonu tanımlar.  `main.py` çalışma
sırasında kombinasyon indeksine göre otomatik rotasyon yapar.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

SIGNATURE_BLOCK = (
    "\n\n---\n"
    "Saygılarımla,\n"
    "Mail Otomasyon Test Sistemi\n"
    "Bilgi Teknolojileri Müdürlüğü\n"
    "Tel: +90 312 000 00 00 | Dahili: 4455\n"
    "Bu mesaj otomatik test altyapısı tarafından üretilmiştir.\n"
)


@dataclass(frozen=True)
class MessageTemplate:
    subject_tag: str
    body: str
    length: str  # "short" | "medium" | "long"


# ────────────────────────────────────────────────────────────────────
#  SENARYO 1: plain_text
# ────────────────────────────────────────────────────────────────────
PLAIN_TEXT_TEMPLATES: List[MessageTemplate] = [
    # --- KISA ---
    MessageTemplate(
        subject_tag="Kısa Bilgilendirme",
        length="short",
        body=(
            "Merhaba,\n\n"
            "Posta sunucusu geçiş testleri kapsamında bu mesaj gönderilmektedir.\n"
            "Türkçe karakter kontrolü: ğüşıöçĞÜŞİÖÇ\n"
            + SIGNATURE_BLOCK
        ),
    ),
    # --- ORTA ---
    MessageTemplate(
        subject_tag="Haftalık Durum Güncellemesi",
        length="medium",
        body=(
            "Merhaba,\n\n"
            "Bu hafta gerçekleştirilen mail sunucu geçiş çalışmalarına ilişkin "
            "güncel durum aşağıdaki gibidir:\n\n"
            "1. SMTP relay yapılandırması tamamlandı.\n"
            "2. IMAP klasör eşleme testleri devam ediyor.\n"
            "3. SPF/DKIM/DMARC kayıtları DNS'e eklendi; yayılım bekleniyor.\n"
            "4. Mobil istemci (iOS/Android) bağlantı testleri planlandı.\n\n"
            "Herhangi bir sorun tespit etmeniz halinde lütfen bu mesajı "
            "yanıtlayarak bildiriniz.\n\n"
            "Türkçe özel karakterler: çalışma, güncelleme, şifreleme, ışık, "
            "ölçüm, üretim — ĞÜŞİÖÇ"
            + SIGNATURE_BLOCK
        ),
    ),
    # --- UZUN ---
    MessageTemplate(
        subject_tag="Safir Posta Geçiş Projesi - Detaylı Teknik Rapor",
        length="long",
        body=(
            "Sayın Yetkili,\n\n"
            "Aşağıda Safir Posta Sunucusu Geçiş Projesi kapsamında yürütülen "
            "teknik çalışmalara ilişkin detaylı raporu bulabilirsiniz.\n\n"

            "== 1. Proje Kapsamı ==\n"
            "Mevcut EMS (Enterprise Mail Server) altyapısından Safir Posta "
            "çözümüne geçiş sürecinde; tüm kullanıcı hesapları, adres "
            "defterleri, takvim verileri ve arşiv posta kutuları taşınacaktır. "
            "Geçiş boyunca mevcut sistem paralel çalışmaya devam edecek, "
            "kullanıcılar kesinti yaşamayacaktır.\n\n"

            "== 2. Tamamlanan İşler ==\n"
            "- MX, SPF, DKIM ve DMARC DNS kayıtları güncellendi.\n"
            "- SMTP relay (port 587/TLS) ve IMAP (port 993/SSL) bağlantıları "
            "doğrulandı.\n"
            "- Active Directory LDAP entegrasyonu ile kullanıcı provisioning "
            "otomatize edildi.\n"
            "- iOS Mail, Android Gmail, Outlook Desktop ve Outlook Web "
            "istemcilerinde bağlantı testleri başarıyla tamamlandı.\n"
            "- 2FA/TOTP kimlik doğrulama akışı entegre edildi.\n\n"

            "== 3. Devam Eden İşler ==\n"
            "- Eklentili mesaj (attachment) iletim testleri farklı dosya "
            "formatlarında (PDF, CSV, DOCX) tekrarlanıyor.\n"
            "- Inline resim (CID embedded) mesajların istemciler arası "
            "görüntülenme uyumluluğu kontrol ediliyor.\n"
            "- Reply chain / thread bütünlüğü testleri çoklu istemci "
            "senaryolarında yürütülüyor.\n\n"

            "== 4. Riskler ve Aksiyonlar ==\n"
            "- Bazı eski istemcilerde UTF-8 encoding uyumsuzluğu "
            "gözlemlendi; charset header'ları güçlendirildi.\n"
            "- Büyük ek dosyalarda (>10 MB) zaman aşımı riski mevcut; "
            "SMTP timeout değeri 60 saniyeye çıkarıldı.\n"
            "- S/MIME dijital imza altyapısı kurumda henüz aktif "
            "olmadığından, bu senaryolar manuel teste bırakıldı.\n\n"

            "Türkçe karakter doğrulama satırı:\n"
            "ğüşıöç ĞÜŞIÖÇ — çalışıyor, güncelleniyor, şifreleniyor, "
            "ışıklandırılıyor, ölçülüyor, üretiliyor.\n\n"

            "Sorularınız için lütfen bu mesajı yanıtlayınız."
            + SIGNATURE_BLOCK
        ),
    ),
]

# ────────────────────────────────────────────────────────────────────
#  SENARYO 2: attachment
# ────────────────────────────────────────────────────────────────────
ATTACHMENT_TEMPLATES: List[MessageTemplate] = [
    # --- KISA ---
    MessageTemplate(
        subject_tag="Ek Dosya İletimi",
        length="short",
        body=(
            "Merhaba,\n\n"
            "İlgili doküman ekte yer almaktadır. İncelemenizi rica ederim.\n"
            "Türkçe karakter: ğüşıöçĞÜŞİÖÇ"
            + SIGNATURE_BLOCK
        ),
    ),
    # --- ORTA ---
    MessageTemplate(
        subject_tag="Talep Sistemi Broşürü ve Gereksinim Dokümanı",
        length="medium",
        body=(
            "Merhaba,\n\n"
            "Ticket Talep Sistemi'ne ait güncel bilgilendirme broşürü ve "
            "dashboard gereksinim dokümanı ekte sunulmuştur.\n\n"
            "Broşürde sistem kullanım adımları, talep oluşturma ve takip "
            "süreçleri açıklanmaktadır. Gereksinim dokümanında ise dashboard "
            "modülü için planlanan bileşenler listelenmiştir.\n\n"
            "Eklerin boyut ve MIME type bilgilerinin alıcıda doğru "
            "görüntülendiğini teyit ediniz.\n\n"
            "Özel karakter testi: çözüm → şifreleme → güncelleme → ışık "
            "→ ölçüm → üretim"
            + SIGNATURE_BLOCK
        ),
    ),
    # --- UZUN ---
    MessageTemplate(
        subject_tag="Safir Posta Destek Raporu ve Ekler",
        length="long",
        body=(
            "Sayın Yetkili,\n\n"
            "Aşağıda Safir Posta geçiş sürecine ilişkin destek raporunun "
            "detayları ve ilgili ek dosyalar yer almaktadır.\n\n"

            "== Ek Dosya Listesi ==\n"
            "1. Ticket_Talep_Sistemi_Bilgilendirme_Brosuru_v3.pdf\n"
            "   → Talep sistemi kullanıcı kılavuzu (PDF, ~500 KB)\n"
            "2. SAFİR POSTA (Destek Bilgem) 2026-01-28.csv\n"
            "   → Destek taleplerinin dönemsel CSV dökümü\n"
            "3. requirements_dashboard.txt\n"
            "   → Dashboard modülü bağımlılık listesi\n\n"

            "== Test Amacı ==\n"
            "Bu mesaj, farklı dosya formatlarının (PDF, CSV, TXT) çoklu "
            "ek olarak gönderildiğinde MIME yapısının, dosya adlarının, "
            "boyutlarının ve Content-Disposition header'larının alıcı "
            "tarafında bozulmadan iletilip iletilmediğini doğrulamak "
            "amacıyla gönderilmektedir.\n\n"

            "Özellikle kontrol edilecek noktalar:\n"
            "- Dosya adındaki Türkçe ve özel karakterler korunuyor mu?\n"
            "  (SAFİR → İ harfi, parantez, boşluk)\n"
            "- PDF dosyası alıcıda açılabiliyor mu?\n"
            "- CSV dosyası metin editörü veya tablo uygulamasında "
            "doğru parse ediliyor mu?\n"
            "- TXT dosyasının encoding'i (UTF-8) bozulmuyor mu?\n\n"

            "Karakter seti doğrulama:\n"
            "ğüşıöç ĞÜŞIÖÇ — tüm Türkçe karakterlerin bu satırda "
            "ve ek dosya adlarında korunması beklenmektedir.\n\n"

            "Testler tamamlandığında sonuçları bu thread üzerinden "
            "paylaşmanızı rica ederim."
            + SIGNATURE_BLOCK
        ),
    ),
]

# ────────────────────────────────────────────────────────────────────
#  SENARYO 3: inline_image
# ────────────────────────────────────────────────────────────────────
INLINE_IMAGE_TEMPLATES: List[MessageTemplate] = [
    # --- KISA ---
    MessageTemplate(
        subject_tag="Ekran Görüntüsü",
        length="short",
        body=(
            "<html><body>"
            "<p>Merhaba,</p>"
            "<p>İlgili ekran görüntüsü aşağıdadır:</p>"
            '<p><img src="cid:{{CID}}" alt="Test Görseli" '
            'style="max-width:600px; border:1px solid #ccc; '
            'border-radius:4px" /></p>'
            "<p>Türkçe: ğüşıöçĞÜŞİÖÇ</p>"
            "{{SIGNATURE_HTML}}"
            "</body></html>"
        ),
    ),
    # --- ORTA ---
    MessageTemplate(
        subject_tag="Dashboard Önizleme Görseli",
        length="medium",
        body=(
            "<html><body>"
            "<p>Merhaba,</p>"
            "<p>Yeni dashboard modülünün ilk tasarım önizlemesi aşağıda "
            "yer almaktadır. Görsel doğrudan mesaj içine gömülmüştür "
            "(inline/CID). Lütfen aşağıdaki kontrolleri yapınız:</p>"
            "<ul>"
            "<li>Görsel mesaj gövdesinde düzgün görüntüleniyor mu?</li>"
            "<li>Çözünürlük ve renkler bozulmamış mı?</li>"
            "<li>Mobil cihazda responsive ölçekleniyor mu?</li>"
            "</ul>"
            '<p><img src="cid:{{CID}}" alt="Dashboard Önizleme" '
            'style="max-width:600px; border:1px solid #ddd; '
            'border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,.1)" /></p>'
            "<p><em>Özel karakterler: çözüm, güncelleme, şifreleme, "
            "ışık, ölçüm, üretim — ĞÜŞİÖÇ</em></p>"
            "{{SIGNATURE_HTML}}"
            "</body></html>"
        ),
    ),
    # --- UZUN ---
    MessageTemplate(
        subject_tag="Posta Geçiş Testi - Inline Görsel Raporu",
        length="long",
        body=(
            "<html><body>"
            "<h2>Safir Posta Geçiş — Inline Görsel İletim Raporu</h2>"
            "<p>Sayın Yetkili,</p>"
            "<p>Bu mesaj, HTML formatındaki e-postalarda gömülü (inline) "
            "görsellerin farklı sunucu ve istemci kombinasyonları arasında "
            "doğru iletilip iletilmediğini test etmek amacıyla "
            "gönderilmektedir.</p>"

            "<h3>Test Detayları</h3>"
            "<table border='1' cellpadding='6' cellspacing='0' "
            "style='border-collapse:collapse; font-family:Arial, sans-serif'>"
            "<tr style='background:#f0f0f0'>"
            "<th>Kontrol Noktası</th><th>Beklenen Sonuç</th></tr>"
            "<tr><td>CID referansı çözümleme</td>"
            "<td>Görsel &lt;img src=\"cid:...\"&gt; ile doğru render</td></tr>"
            "<tr><td>Content-Type header</td>"
            "<td>image/png veya image/jpeg</td></tr>"
            "<tr><td>Content-Disposition</td>"
            "<td>inline; filename=\"...\"</td></tr>"
            "<tr><td>Boyut/Çözünürlük</td>"
            "<td>Orijinal piksel değerleri korunmalı</td></tr>"
            "<tr><td>Alternatif metin</td>"
            "<td>HTML desteklemeyen istemcilerde fallback text</td></tr>"
            "</table>"

            "<h3>Gömülü Görsel</h3>"
            '<p><img src="cid:{{CID}}" alt="Test Görseli — Safir Posta" '
            'style="max-width:600px; border:2px solid #0066cc; '
            'border-radius:8px; padding:4px" /></p>'

            "<p>Yukarıdaki görsel görünmüyorsa, CID referansı alıcı "
            "tarafında çözümlenememiş demektir. Bu durumda istemci "
            "loglarını ve MIME yapısını kontrol ediniz.</p>"

            "<h3>Türkçe Karakter Doğrulama</h3>"
            "<p>ğüşıöç ĞÜŞIÖÇ — çalışıyor, güncelleniyor, şifreleniyor, "
            "ışıklandırılıyor, ölçülüyor, üretiliyor.</p>"

            "{{SIGNATURE_HTML}}"
            "</body></html>"
        ),
    ),
]

# ────────────────────────────────────────────────────────────────────
#  SENARYO 4: reply_chain
# ────────────────────────────────────────────────────────────────────
REPLY_CHAIN_TEMPLATES: List[MessageTemplate] = [
    # --- KISA ---
    MessageTemplate(
        subject_tag="Durum Onayı",
        length="short",
        body=(
            "Teşekkürler, not aldım. İşlem tamamlandığında "
            "bilgilendireceğim.\n"
            "Karakter testi: ğüşıöçĞÜŞİÖÇ"
            + SIGNATURE_BLOCK
        ),
    ),
    # --- ORTA ---
    MessageTemplate(
        subject_tag="Geçiş Testi Geri Bildirimi",
        length="medium",
        body=(
            "Merhaba,\n\n"
            "Önceki mesajınızdaki test sonuçlarını inceledim. Aşağıdaki "
            "noktaları paylaşmak isterim:\n\n"
            "- SMTP bağlantısı sorunsuz görünüyor; relay loglarında "
            "hata kaydı yok.\n"
            "- Eklenti iletiminde dosya adı Türkçe karakter içerdiğinde "
            "(ör. 'SAFİR') bazı istemcilerde encoding farkı oluşabiliyor. "
            "Content-Disposition header'ındaki filename* parametresini "
            "RFC 5987 uyumlu şekilde güncellememiz gerekebilir.\n"
            "- Thread yapısı korunuyor; In-Reply-To ve References "
            "header'ları doğru.\n\n"
            "Bu konuda ek test yapılmasını önerir misiniz?\n"
            "Özel karakterler: ğüşıöçĞÜŞİÖÇ"
            + SIGNATURE_BLOCK
        ),
    ),
    # --- UZUN ---
    MessageTemplate(
        subject_tag="Detaylı Reply Chain Analizi",
        length="long",
        body=(
            "Merhaba,\n\n"
            "Thread üzerindeki tüm mesajları sırasıyla inceledim. "
            "Aşağıda kombinasyon bazlı gözlemlerimi detaylı olarak "
            "aktarıyorum:\n\n"

            "== Gözlem 1: Header Bütünlüğü ==\n"
            "İlk mesajdan itibaren Message-ID, In-Reply-To ve References "
            "header'ları zincirleme olarak doğru taşınmış. Thread "
            "görünümü hem Outlook Desktop hem Gmail Web'de düzgün.\n\n"

            "== Gözlem 2: Alıntı (Quote) Yapısı ==\n"
            "Gmail, alıntıyı '>' prefix ile text/plain part'ta tutuyor. "
            "Outlook ise HTML blockquote kullanıyor. İki format arasında "
            "geçiş yapıldığında bazı satır sonlarında (CRLF vs LF) "
            "farklılık gözlendi, ancak içerik kaybı yok.\n\n"

            "== Gözlem 3: Encoding Uyumu ==\n"
            "Türkçe karakterler (ğüşıöç) tüm cevaplarda korunmuş. "
            "Özellikle Subject satırındaki '=?UTF-8?B?...' veya "
            "'=?UTF-8?Q?...' kodlaması doğru decode ediliyor.\n\n"

            "== Gözlem 4: Çoklu İstemci Senaryosu ==\n"
            "Mesaj akışı: EMS/iOS → Gmail/Android → Outlook/Desktop "
            "sırasıyla yanıtlandığında thread bütünlüğü korunuyor. "
            "Ancak 4. seviye derinlikten itibaren bazı istemcilerin "
            "alıntıyı collapse ettiği görüldü — bu beklenen bir "
            "davranıştır.\n\n"

            "== Sonuç ==\n"
            "Reply chain senaryosu genel olarak PASS durumundadır. "
            "Küçük format farklılıkları production kullanımını "
            "etkilememektedir.\n\n"

            "Karakter doğrulama: ğüşıöç ĞÜŞIÖÇ — çalışıyor, "
            "güncelleniyor, şifreleniyor, ışıklandırılıyor, "
            "ölçülüyor, üretiliyor."
            + SIGNATURE_BLOCK
        ),
    ),
]

# ────────────────────────────────────────────────────────────────────
#  SENARYO 5: reply_chain orijinal mesaj (reply'den önce gönderilen)
# ────────────────────────────────────────────────────────────────────
REPLY_ORIGINAL_TEMPLATES: List[MessageTemplate] = [
    MessageTemplate(
        subject_tag="Test Koordinasyonu",
        length="short",
        body=(
            "Merhaba, posta geçiş testi başlatılıyor. "
            "Sonuçları bu thread üzerinden paylaşacağız.\n"
            "Karakter testi: ğüşıöçĞÜŞİÖÇ"
            + SIGNATURE_BLOCK
        ),
    ),
    MessageTemplate(
        subject_tag="Geçiş Testi Başlangıç",
        length="medium",
        body=(
            "Merhaba,\n\n"
            "Safir Posta geçiş testleri bu mesaj ile başlatılmıştır. "
            "Lütfen aşağıdaki adımları takip ediniz:\n\n"
            "1. Mesajın eksiksiz ulaştığını teyit edin.\n"
            "2. Bu mesajı yanıtlayarak thread testi başlatın.\n"
            "3. Yanıtınızda Türkçe karakter kullanmayı unutmayın.\n\n"
            "Karakter doğrulama: ğüşıöç ĞÜŞIÖÇ"
            + SIGNATURE_BLOCK
        ),
    ),
    MessageTemplate(
        subject_tag="Posta Sunucu Testi Başlangıç Bildirimi",
        length="long",
        body=(
            "Sayın Yetkili,\n\n"
            "Bu mesaj, Safir Posta sunucu geçişi kapsamında yürütülen "
            "reply chain (yanıt zinciri) testinin başlangıç mesajıdır.\n\n"
            "Testin amacı; farklı mail sunucuları ve istemciler arasında "
            "mesaj yanıtlandığında thread bütünlüğünün, encoding "
            "doğruluğunun ve alıntı yapısının korunup korunmadığını "
            "doğrulamaktır.\n\n"
            "Lütfen bu mesajı yanıtlayınız. Yanıtınızda:\n"
            "- Türkçe özel karakterler kullanınız (ğüşıöçĞÜŞİÖÇ)\n"
            "- Birden fazla paragraf yazınız\n"
            "- Mümkünse farklı bir istemciden yanıtlayınız\n\n"
            "Karakter doğrulama satırı:\n"
            "ğüşıöç ĞÜŞIÖÇ — çalışma, güncelleme, şifreleme, ışık, "
            "ölçüm, üretim."
            + SIGNATURE_BLOCK
        ),
    ),
]


SIGNATURE_HTML = (
    "<hr style='border:none; border-top:1px solid #ccc; margin:24px 0 12px' />"
    "<p style='font-size:12px; color:#666; font-family:Arial, sans-serif'>"
    "<strong>Mail Otomasyon Test Sistemi</strong><br/>"
    "Bilgi Teknolojileri Müdürlüğü<br/>"
    "Tel: +90 312 000 00 00 | Dahili: 4455<br/>"
    "<em>Bu mesaj otomatik test altyapısı tarafından üretilmiştir.</em>"
    "</p>"
)


# ────────────────────────────────────────────────────────────────────
#  Yardımcılar
# ────────────────────────────────────────────────────────────────────
ALL_TEMPLATES: Dict[str, List[MessageTemplate]] = {
    "plain_text": PLAIN_TEXT_TEMPLATES,
    "attachment": ATTACHMENT_TEMPLATES,
    "inline_image": INLINE_IMAGE_TEMPLATES,
    "reply_chain": REPLY_CHAIN_TEMPLATES,
}


def get_template(scenario_key: str, rotation_index: int) -> MessageTemplate:
    """Senaryo ve rotasyon indeksine göre kısa/orta/uzun şablondan birini döndürür."""
    templates = ALL_TEMPLATES.get(scenario_key, PLAIN_TEXT_TEMPLATES)
    return templates[rotation_index % len(templates)]


def get_reply_original(rotation_index: int) -> MessageTemplate:
    """Reply chain orijinal mesajı için şablon döndürür."""
    return REPLY_ORIGINAL_TEMPLATES[rotation_index % len(REPLY_ORIGINAL_TEMPLATES)]


def resolve_inline_html(html: str, cid: str) -> str:
    """Inline image şablonundaki {{CID}} ve {{SIGNATURE_HTML}} yer tutucularını doldurur."""
    return html.replace("{{CID}}", cid).replace("{{SIGNATURE_HTML}}", SIGNATURE_HTML)
