"""
message_templates.py — Senaryo bazlı e-posta taslak mesajları.

Her senaryo tipi için kısa / orta / uzun olmak üzere 3 farklı uzunlukta
gerçekçi Türkçe iş yazışması şablonu tanımlar.  `main.py` çalışma
sırasında kombinasyon indeksine göre otomatik rotasyon yapar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
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
    attachments: list[str] | None = None  # filenames in attachments/


# ────────────────────────────────────────────────────────────────────
#  SENARYO 1: plain_text
# ────────────────────────────────────────────────────────────────────
PLAIN_TEXT_TEMPLATES: List[MessageTemplate] = [
    # --- KISA ---
    MessageTemplate(
        subject_tag="Düz Metin İletim Testi",
        length="short",
        body=(
            "Merhaba,\n\n"
            "Bu mesaj, alıcı istemcinin temel SMTP düz metin (Plain Text) e-postaları "
            "UTF-8 standartlarında alıp almadığını doğrulamak amacıyla gönderilmiştir.\n"
            "Test Amacı: Karakter seti bütünlüğü ve iletim hızı.\n"
            "Türkçe karakterler: ğüşıöçĞÜŞİÖÇ\n"
            + SIGNATURE_BLOCK
        ),
    ),
    # --- ORTA ---
    MessageTemplate(
        subject_tag="Protokol Doğrulama - UTF-8 / Plain Text",
        length="medium",
        body=(
            "Sayın Yetkili,\n\n"
            "E-posta altyapı geçiş çalışmaları kapsamında, alıcı istemcinin "
            "standard RFC 5322 uyumluluğu test edilmektedir.\n\n"
            "Senaryo Detayı:\n"
            "- Mesaj Tipi: Plain Text (Düz Metin)\n"
            "- Kodlama: UTF-8 (7-bit / 8-bit)\n"
            "- Kontrol: ĞÜŞİÖÇ karakterlerinin bozulmadan iletimi.\n\n"
            "Bu mesajın içeriği, sunucu tarafındaki MIME transformasyonlarının "
            "doğruluğunu teyit etmek için tasarlanmıştır.\n"
            + SIGNATURE_BLOCK
        ),
    ),
    # --- UZUN ---
    MessageTemplate(
        subject_tag="Teknik Analiz: Plain Text Protokol Uyumluluğu",
        length="long",
        body=(
            "Sayın Teknik Ekip,\n\n"
            "Posta sunucusu (EMS/Safir) geçiş sürecinde, alıcı tarafındaki istemcilerin "
            "metin tabanlı içerikleri işleme kapasitesi ölçülmektedir.\n\n"
            "== Test ve Analiz Kapsamı ==\n"
            "Bu uzun formatlı düz metin mesajı; ağ üzerindeki paket segmentasyonu ve "
            "SMTP sunucusundaki satır sonu (CRLF) dönüşümlerini test eder. "
            "Özellikle 80 karakterden uzun satırların istemci tarafından nasıl "
            "wrap edildiği (sarılmış) ve Türkçe karakterlerin bu süreçte "
            "zarar görüp görmediği incelenmektedir.\n\n"
            "== Doğrulama Parametreleri ==\n"
            "1. UTF-8 Karakter seti: ğüşıöç ĞÜŞIÖÇ (Tam Set)\n"
            "2. Satır uzunluğu toleransı: Mesajın görsel bütünlüğü korunmalıdır.\n"
            "3. Header / Body ayırımı devitasyonu: MIME boundary tespiti.\n\n"
            "Bu otomatik üretilmiş bir içeriktir. Lütfen kontrol sonrası teyit ediniz."
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
        subject_tag="Ek Dosya Protokol Testi",
        length="short",
        body=(
            "Merhaba,\n\n"
            "Bu mesaj, alıcı istemcinin 'Content-Disposition' header'larını ve "
            "Base64 kodlanmış eklentileri doğru işleyip işlemediğini test eder.\n"
            "Test Amacı: Dosya iletim bütünlüğü.\n"
            "Türkçe karakter: ğüşıöçĞÜŞİÖÇ"
            + SIGNATURE_BLOCK
        ),
    ),
    # --- ORTA ---
    MessageTemplate(
        subject_tag="Eklenti İletim Kararlılık Sınaması",
        length="medium",
        body=(
            "Sayın Yetkili,\n\n"
            "Posta altyapısı testleri kapsamında eklenti iletimi gerçekleşmektedir.\n\n"
            "Kontrol Noktaları:\n"
            "- Eklenti dosya adındaki Türkçe karakterlerin (ğüşıöç) korunması.\n"
            "- MIME boundary (sınır) belirteçlerinin alıcıda doğru ayrıştırılması.\n"
            "- Content-Transfer-Encoding: base64 doğrulaması.\n\n"
            "Lütfen ekteki dosyanın ismini ve boyutunu kontrol ediniz."
            + SIGNATURE_BLOCK
        ),
    ),
    # --- UZUN ---
    MessageTemplate(
        subject_tag="Teknik Rapor: Eklenti ve MIME Yapısı Analizi",
        length="long",
        body=(
            "Teknik Ekibe,\n\n"
            "Bu mesaj, e-posta istemcilerinin ekli dosyaları (attachment) RFC 2183 "
            "standartlarına göre nasıl kapsüllediğini ölçümlemek için tasarlanmıştır.\n\n"
            "== Senaryo ve Beklenti ==\n"
            "E-posta içeriğinde yer alan ekler; sunucu tarafından 'multipart/mixed' "
            "yapısı altında gönderilmektedir. Testin temel amacı, bu karışık MIME "
            "segmentlerinin alıcı tarafında (Outlook, Gmail, iOS vb.) birbirinden "
            "hatasız ayrıştırılıp ayrıştırılmadığını görmektir. \n\n"
            "== Doğrulama Listesi ==\n"
            "- Dosya Adı: {filename} (Türkçe karakter testi dahil)\n"
            "- MIME Type: Alıcı tarafından otomatik tanınmalıdır.\n"
            "- İletim Güvenliği: Dosyanın binary bütünlüğü korunmalıdır.\n\n"
            "Özellikle mobil istemcilerde (EAS/Z-Push) eklenti adlarındaki "
            "bozulmalar (encoding errors) bu testin ana odak noktasıdır."
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
        subject_tag="Gömülü Görsel (CID) Doğrulaması",
        length="short",
        body=(
            "<html><body>"
            "<p>Merhaba,</p>"
            "<p>Bu mesaj, HTML e-postalarda 'Content-ID' (CID) referanslı gömülü "
            "görsellerin iletimini test etmek içindir.</p>"
            '<p><img src="cid:{{CID}}" alt="Gömülü Görsel" style="max-width:600px" /></p>'
            "<p>Türkçe: ğüşıöçĞÜŞİÖÇ</p>"
            "{{SIGNATURE_HTML}}"
            "</body></html>"
        ),
    ),
    # --- ORTA ---
    MessageTemplate(
        subject_tag="HTML CID Uyumluluk Sınaması",
        length="medium",
        body=(
            "<html><body>"
            "<h3>Gömülü Görsel Testi</h3>"
            "<p>E-posta istemcinizin inline (mesaj gövdesinde) görselleri işleme kapasitesi "
            "aşağıdaki görsel üzerinden test edilmektedir.</p>"
            "<ul>"
            "<li>Görsel multipart/related yapısı içinde kapsüllenmiştir.</li>"
            "<li>Farklı ağ koşullarında görsel bütünlüğü korunmalıdır.</li>"
            "</ul>"
            "<p>Yukarıdaki görsel görünmüyorsa, CID referansı alıcı "
            "tarafında çözümlenememiş demektir. Bu durumda istemci "
            "loglarını ve MIME yapısını kontrol ediniz.</p>"
            '<h3>Gömülü Görsel</h3><p><img src="cid:{{CID}}" alt="Safir Test" style="max-width:600px; border:1px solid #ccc" /></p>'
            "{{SIGNATURE_HTML}}"
            "</body></html>"
        ),
    ),
    # --- UZUN ---
    MessageTemplate(
        subject_tag="Gelişmiş CID ve HTML5 Render Analizi",
        length="long",
        body=(
            "<html><body>"
            "<h2>Teknik Detay: CID (Content-ID) Mimarisi</h2>"
            "<p>Bu senaryo, görsellerin base64 olarak gövdeye gömülmesi yerine, "
            "ayrı bir MIME parçasında tutulup HTML içinde <code>cid:</code> protocolu "
            "ile çağırılmasını (referencing) test eder.</p>"
            "== Beklenen Davranış ==<br/>"
            "1. İstemci mesajı açtığında görseli otomatik yüklemelidir (external image gibi blocklamadan).<br/>"
            "2. 'Forward' veya 'Reply' yapıldığında CID eşleşmesi bozulmamalıdır.<br/>"
            "3. Outlook'un 'Word' motorunda ve mobil istemcilerde 'auto-scale' özelliği düzgün çalışmalıdır.<br/>"
            "<br/>"
            '<h3>Test Görseli</h3><p><img src="cid:{{CID}}" alt="Kurumsal Logo" style="max-width:100%; height:auto" /></p>'
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
            "bilgilendireceğim.\n\n"
            "Karakter testi: ğüşıöç ĞÜŞİÖÇ"
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
            "- SMTP bağlantısı sorunsuz görünüyor; relay loglarında hata yok.\n"
            "- Eklenti iletiminde dosya adı Türkçe karakter içerdiğinde (ör. 'SAFİR') "
            "bazı istemcilerde encoding farkı oluşabiliyor.\n"
            "- Thread yapısı korunuyor; In-Reply-To header'ı doğru.\n\n"
            "Karakter testi: ğüşıöçĞÜŞİÖÇ"
            + SIGNATURE_BLOCK
        ),
    ),
    # --- UZUN ---
    MessageTemplate(
        subject_tag="Detaylı Reply Chain Analizi ve Karar",
        length="long",
        body=(
            "Sayın Yetkili,\n\n"
            "Thread üzerindeki tüm mesajları sırasıyla inceledim. "
            "Aşağıda kombinasyon bazlı gözlemlerimi detaylı olarak aktarıyorum:\n\n"
            "== Gözlem 1: Header Bütünlüğü ==\n"
            "İlk mesajdan itibaren Message-ID ve References header'ları "
            "zincirleme olarak doğru taşınmış. Thread görünümü hatasız.\n\n"
            "== Gözlem 2: Alıntı (Quote) Yapısı ==\n"
            "İstemciler arasındaki alıntı (quote) formatı geçişlerinde "
            "karakter kaybı yaşanmıyor.\n\n"
            "Karakter doğrulama: ğüşıöç ĞÜŞIÖÇ — çalışma, güncelleme, şifreleme."
            + SIGNATURE_BLOCK
        ),
    ),
]

# ────────────────────────────────────────────────────────────────────
#  SENARYO 5: reply_chain orijinal mesaj
# ────────────────────────────────────────────────────────────────────
REPLY_ORIGINAL_TEMPLATES: List[MessageTemplate] = [
    MessageTemplate(
        subject_tag="Test Koordinasyonu",
        length="short",
        body="Merhaba, posta geçiş testi başlatılıyor. " + SIGNATURE_BLOCK,
    ),
    MessageTemplate(
        subject_tag="Geçiş Testi Başlangıç",
        length="medium",
        body="Merhaba,\n\nSafir Posta geçiş testleri bu mesaj ile başlatılmıştır." + SIGNATURE_BLOCK,
    ),
    MessageTemplate(
        subject_tag="Posta Sunucu Testi Başlangıç Bildirimi",
        length="long",
        body=(
            "Sayın Yetkili,\n\n"
            "Bu mesaj, Safir Posta sunucu geçişi kapsamında yürütülen "
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
#  SENARYO 6: html_table (Yeni!)
# ────────────────────────────────────────────────────────────────────
TABLE_TEMPLATES: List[MessageTemplate] = [
    MessageTemplate(
        subject_tag="HTML Tablo Render Testi (3x3)",
        length="short",
        body=(
            "<html><body>"
            "<p>Merhaba, bu mesaj istemcinin HTML tablolarını rendering kapasitesini ölçer.</p>"
            "<table border='1' style='border-collapse:collapse; width:100%'>"
            "<tr><th>Parametre</th><th>Değer</th><th>Durum</th></tr>"
            "<tr><td>Karakter Seti</td><td>UTF-8</td><td>✅ Başarılı</td></tr>"
            "<tr><td>MIME Tip</td><td>HTML</td><td>✅ Başarılı</td></tr>"
            "</table>"
            "{{SIGNATURE_HTML}}"
            "</body></html>"
        ),
    ),
    MessageTemplate(
        subject_tag="Kompleks HTML Tablo ve Stil Doğrulaması",
        length="medium",
        body=(
            "<html><body>"
            "<h3>MIME Tablo Uyumluluk Testi</h3>"
            "<p>Aşağıdaki tablo, hücre içi stillerin ve arka plan renklerinin korunup korunmadığını test eder.</p>"
            "<table style='width:100%; border-collapse:collapse; border:1px solid #aaa'>"
            "<thead><tr style='background:#004a99; color:white'>"
            "<th style='padding:10px'>ID</th><th style='padding:10px'>Kategori</th><th style='padding:10px'>Test Notu</th></tr></thead>"
            "<tbody>"
            "<tr><td style='padding:10px; border-bottom:1px solid #ddd'>101</td>"
            "<td style='padding:10px; border-bottom:1px solid #ddd'>Protokol</td>"
            "<td style='padding:10px; border-bottom:1px solid #ddd'>ğüşıöç karakter seti testi</td></tr>"
            "<tr style='background:#f9f9f9'><td style='padding:10px; border-bottom:1px solid #ddd'>102</td>"
            "<td style='padding:10px; border-bottom:1px solid #ddd'>Görünüm</td>"
            "<td style='padding:10px; border-bottom:1px solid #ddd'>Zebra-striping render kontrolü</td></tr>"
            "</tbody>"
            "</table>"
            "{{SIGNATURE_HTML}}"
            "</body></html>"
        ),
    ),
    MessageTemplate(
        subject_tag="Kritik Veri Tablosu - Render ve Bütünlük",
        length="long",
        body=(
            "<html><body>"
            "<h2>Teknik Test: Gelişmiş HTML Tablo Yapısı</h2>"
            "<p>Bu senaryo, kurumsal yazışmalarda sıkça kullanılan 'tablo kopyala-yapıştır' (table paste) davranışını e-posta protokolü seviyesinde simüle eder.</p>"
            "<table style='border:2px solid #333; font-family:sans-serif; width:100%; border-collapse:collapse'>"
            "<tr><th colspan='3' style='background:#333; color:white; padding:15px'>Sistem Metrikleri Raporu</th></tr>"
            "<tr><td style='padding:10px; border:1px solid #666'><b>CPU</b></td><td style='padding:10px; border:1px solid #666'>%12</td><td style='padding:10px; border:1px solid #666; color:green'>Stabil</td></tr>"
            "<tr><td style='padding:10px; border:1px solid #666'><b>RAM</b></td><td style='padding:10px; border:1px solid #666'>4.2 GB</td><td style='padding:10px; border:1px solid #666; color:orange'>Orta</td></tr>"
            "<tr><td style='padding:10px; border:1px solid #666'><b>IMAP I/O</b></td><td style='padding:10px; border:1px solid #666'>240 msg/s</td><td style='padding:10px; border:1px solid #666; color:blue'>Yüksek</td></tr>"
            "</table>"
            "<p>Türkçe Karakter: ğüşıöçĞÜŞİÖÇ</p>"
            "{{SIGNATURE_HTML}}"
            "</body></html>"
        ),
    ),
]

# ────────────────────────────────────────────────────────────────────
#  SENARYO 7: forward_chain (Yeni!)
# ────────────────────────────────────────────────────────────────────
FORWARD_CHAIN_TEMPLATES: List[MessageTemplate] = [
    MessageTemplate(
        subject_tag="Fwd: Protokol Bilgilendirme",
        length="short",
        body=(
            "İletilen mesajın (Forward) MIME boundary izolasyonunu ve "
            "header bütünlüğünü test eder.\n\n"
            "Türkçe: ğüşıöçĞÜŞİÖÇ"
            + SIGNATURE_BLOCK
        ),
    ),
    MessageTemplate(
        subject_tag="Fwd: Önemli Duyuru ve Teknik Detay",
        length="medium",
        body=(
            "Merhaba,\n\n"
            "Aşağıdaki mesajın iletim (forwarding) sürecinde MIME kapsüllemesinin "
            "bozulmadığını teyit ediniz. Orijinal 'From' ve 'Date' bilgileri "
            "gövdede net okunabilmelidir.\n\n"
            "--- Orijinal Mesaj ---\n"
            "Amaç: Forward zinciri protokol uyumluluğu.\n"
            "Sınama: ğüşıöçĞÜŞİÖÇ"
            + SIGNATURE_BLOCK
        ),
    ),
    MessageTemplate(
        subject_tag="Fwd: Fwd: Teknik Analiz ve Posta Yönetimi",
        length="long",
        body=(
            "Sayın Yönetici,\n\n"
            "Bu mesaj, 'Nested Forward' (İç İçe İletme) yapısını test etmekte "
            "olup, RFC standardına göre boundary iç içe geçmelerini (nesting) "
            "denetler.\n\n"
            "== Test Kapsamı ==\n"
            "Çoklu iletme işlemlerinde mesaj hiyerarşisinin hantal istemcilerde "
            "dahi bozulmadan (özellikle encoding ve özel karakterlerde) "
            "iletimi en kritik noktadır.\n\n"
            "Karakter Doğrulama: ğüşıöç ĞÜŞIÖÇ — çalışma, güncelleme, şifreleme."
            + SIGNATURE_BLOCK
        ),
    ),
]

# ────────────────────────────────────────────────────────────────────
#  SENARYO 8: calendar_invite
# ────────────────────────────────────────────────────────────────────
CALENDAR_INVITE_TEMPLATES: List[MessageTemplate] = [
    MessageTemplate(subject_tag="Toplantı: Haftalık Senkron", length="short", body="Haftalık teknik senkron toplantısı davetidir." + SIGNATURE_BLOCK),
    MessageTemplate(subject_tag="Workshop: Safir Posta Eğitimi", length="medium", body="Merhaba,\n\nSafir Posta geçişi öncesi son kullanıcı eğitimi yapılacaktır." + SIGNATURE_BLOCK),
    MessageTemplate(subject_tag="Proje Değerlendirme Kurulu (Kurumsal)", length="long", body="Sayın Paydaşlar,\n\nProjenin final aşamasına geçilmesi öncesinde detaylı bir değerlendirme toplantısı planlanmıştır." + SIGNATURE_BLOCK),
]

# ────────────────────────────────────────────────────────────────────
#  SENARYO 9: i18n
# ────────────────────────────────────────────────────────────────────
I18N_TEMPLATES: List[MessageTemplate] = [
    MessageTemplate(subject_tag="🌍 Test: ÖÇŞĞÜİ 🚀", length="short", body="Türkçe: ÖÇŞĞÜİöçşğüı (şifreleme, çalışma)." + SIGNATURE_BLOCK),
    MessageTemplate(subject_tag="🌍 Multi-lingual: ﷽ 漢字 🔥", length="medium", body="Arapça: ﷽\nAsya: 漢字 (Test)\nEmoji: 🚀🔥🐞\nTürkçe: öçşğüı" + SIGNATURE_BLOCK),
    MessageTemplate(subject_tag="🌍 Uluslararası Karakter Bütünlüğü Analizi", length="long", body="Bu test, farklı alfabelerin (UTF-8) sunucu ve istemci tarafında bozulmadan taşınmasını ölçer.\n\nÖzel: ğüşıöç ĞÜŞIÖÇ\nArapça: ﷽\nAsya: 漢字\nEmoji: 🚀🔥🐞" + SIGNATURE_BLOCK),
]

# ────────────────────────────────────────────────────────────────────
#  SENARYO 10: complex_html — Rich CSS ve Media Query
# ────────────────────────────────────────────────────────────────────
# Not: Bu senaryo daha önce INLINE_IMAGE_TEMPLATES'i yeniden kullanıyordu;
# o şablonlar {{CID}} yer tutucusu içerdiği için resimsiz HTML mesajda
# çözümlenmeden kalıyordu. Bu yüzden kendi şablon seti tanımlandı.
COMPLEX_HTML_TEMPLATES: List[MessageTemplate] = [
    MessageTemplate(
        subject_tag="Duyarlı (Responsive) HTML Render Testi",
        length="short",
        body=(
            "<html><head><style>"
            ".kart{background:#f4f7fb;border-left:4px solid #004a99;padding:16px;"
            "font-family:Arial,Helvetica,sans-serif}"
            ".vurgu{color:#004a99;font-weight:700}"
            "@media only screen and (max-width:480px){"
            ".kart{padding:8px;font-size:13px} .gizle-mobil{display:none !important}}"
            "</style></head><body>"
            "<div class='kart'>"
            "<p class='vurgu'>Zengin CSS Render Sınaması</p>"
            "<p>Bu mesaj, istemcinin <code>&lt;style&gt;</code> bloğunu ve CSS "
            "sınıflarını koruyup korumadığını ölçer.</p>"
            "<p class='gizle-mobil'>Bu satır yalnızca masaüstünde görünmelidir "
            "(media query testi).</p>"
            "<p>Türkçe: ğüşıöçĞÜŞİÖÇ</p>"
            "</div>"
            "{{SIGNATURE_HTML}}"
            "</body></html>"
        ),
    ),
    MessageTemplate(
        subject_tag="CSS Media Query ve Kutu Modeli Doğrulaması",
        length="medium",
        body=(
            "<html><head><style>"
            "body{margin:0;background:#eef2f7}"
            ".sarmal{max-width:600px;margin:0 auto;background:#ffffff;"
            "font-family:'Segoe UI',Arial,sans-serif}"
            ".baslik{background:linear-gradient(90deg,#004a99,#0077cc);"
            "color:#fff;padding:20px}"
            ".govde{padding:20px;line-height:1.6}"
            ".rozet{display:inline-block;background:#e8f4ff;color:#004a99;"
            "border-radius:12px;padding:4px 12px;font-size:12px}"
            "@media only screen and (max-width:480px){"
            ".sarmal{width:100% !important} .baslik{padding:12px}"
            ".govde{padding:12px;font-size:14px}}"
            "</style></head><body>"
            "<div class='sarmal'>"
            "<div class='baslik'><h2 style='margin:0'>Kurumsal Bülten Şablonu</h2></div>"
            "<div class='govde'>"
            "<span class='rozet'>Test</span> <span class='rozet'>Render</span>"
            "<p>Bu şablon; gradient arka plan, yuvarlatılmış rozetler ve "
            "<strong>max-width</strong> tabanlı duyarlı yerleşim içerir.</p>"
            "<p>Bazı istemciler (Outlook Desktop) <code>&lt;style&gt;</code> "
            "bloğunu kaldırır; bu durumda düzen bozulur ancak metin okunabilir "
            "kalmalıdır.</p>"
            "<p>Karakter Doğrulama: ğüşıöç ĞÜŞİÖÇ</p>"
            "</div></div>"
            "{{SIGNATURE_HTML}}"
            "</body></html>"
        ),
    ),
    MessageTemplate(
        subject_tag="Gelişmiş CSS Yerleşim ve Yedekleme (Fallback) Analizi",
        length="long",
        body=(
            "<html><head><style>"
            "body{margin:0;padding:0;background:#e9edf2}"
            ".dis{width:100%;padding:24px 0}"
            ".ic{max-width:640px;margin:0 auto;background:#fff;border-radius:8px;"
            "overflow:hidden;font-family:Arial,sans-serif;color:#222}"
            ".ust{background:#12263f;color:#fff;padding:24px}"
            ".sutunlar{display:flex;gap:16px;padding:20px}"
            ".sutun{flex:1;background:#f6f8fa;padding:14px;border-radius:6px}"
            ".alt{background:#f0f3f7;padding:16px;font-size:12px;color:#555}"
            "@media only screen and (max-width:520px){"
            ".sutunlar{display:block} .sutun{margin-bottom:12px}"
            ".ust{padding:14px} .ic{border-radius:0}}"
            "</style></head><body>"
            "<div class='dis'><div class='ic'>"
            "<div class='ust'><h1 style='margin:0;font-size:22px'>"
            "Teknik Rapor: HTML Uyumluluk</h1>"
            "<p style='margin:6px 0 0;opacity:.85'>Rich CSS ve Media Query Sınaması</p></div>"
            "<div class='sutunlar'>"
            "<div class='sutun'><strong>Flexbox</strong><p>Çok sütunlu yerleşim; "
            "desteklenmeyen istemcide alt alta düşmelidir.</p></div>"
            "<div class='sutun'><strong>Media Query</strong><p>520px altında tek "
            "sütuna geçer.</p></div>"
            "<div class='sutun'><strong>Tipografi</strong><p>ğüşıöç ĞÜŞİÖÇ "
            "karakterleri bozulmamalıdır.</p></div>"
            "</div>"
            "<div class='alt'>Bu mesaj otomatik test altyapısı tarafından "
            "üretilmiştir. Görüntüleme sorunlarında düz metin alternatifi "
            "(text/plain) kullanılmalıdır.</div>"
            "</div></div>"
            "{{SIGNATURE_HTML}}"
            "</body></html>"
        ),
    ),
]

# ────────────────────────────────────────────────────────────────────
#  SENARYO 11: multi_attachment — Çoklu ek
# ────────────────────────────────────────────────────────────────────
MULTI_ATTACHMENT_TEMPLATES: List[MessageTemplate] = [
    MessageTemplate(
        subject_tag="Çoklu Ek İletim Testi",
        length="short",
        body=(
            "Merhaba,\n\n"
            "Bu mesaj birden fazla ek dosya içermektedir. Tüm eklerin "
            "eksiksiz iletilip iletilmediğini kontrol ediniz.\n\n"
            "Türkçe: ğüşıöçĞÜŞİÖÇ"
            + SIGNATURE_BLOCK
        ),
    ),
    MessageTemplate(
        subject_tag="Karma Formatlı Çoklu Ek Doğrulaması",
        length="medium",
        body=(
            "Merhaba,\n\n"
            "Ekte farklı formatlarda dosyalar bulunmaktadır. Kontrol noktaları:\n"
            "  • Her ekin adı ve uzantısı korunmuş mu?\n"
            "  • Toplam boyut sunucu limitine takılmış mı?\n"
            "  • MIME type'lar doğru atanmış mı?\n\n"
            "Karakter sınaması: ğüşıöç ĞÜŞİÖÇ"
            + SIGNATURE_BLOCK
        ),
    ),
    MessageTemplate(
        subject_tag="Toplu Doküman Aktarımı ve Bütünlük Denetimi",
        length="long",
        body=(
            "Sayın Yetkili,\n\n"
            "Bu mesaj, kurumsal yazışmalarda sıkça karşılaşılan çoklu doküman "
            "aktarımı senaryosunu simüle etmektedir. multipart/mixed yapısı "
            "içinde birden fazla ek taşınmakta olup, aşağıdaki hususlar "
            "denetlenmelidir:\n\n"
            "== Denetim Kapsamı ==\n"
            "1. Ek sayısının gönderim ile birebir örtüşmesi\n"
            "2. Dosya adlarındaki Türkçe karakterlerin (RFC 2231) korunması\n"
            "3. base64 kodlamasının bozulmadan çözülebilmesi\n"
            "4. Toplam mesaj boyutunun sunucu eşiğini aşmaması\n\n"
            "Karakter Doğrulama: ğüşıöç ĞÜŞIÖÇ — çalışma, güncelleme, şifreleme."
            + SIGNATURE_BLOCK
        ),
    ),
]

# ────────────────────────────────────────────────────────────────────
#  Yardımcılar
# ────────────────────────────────────────────────────────────────────
ALL_TEMPLATES: Dict[str, List[MessageTemplate]] = {
    "plain_text": PLAIN_TEXT_TEMPLATES,
    "attachment": ATTACHMENT_TEMPLATES,
    "multi_attachment": MULTI_ATTACHMENT_TEMPLATES,
    "inline_image": INLINE_IMAGE_TEMPLATES,
    "reply_chain": REPLY_CHAIN_TEMPLATES,
    "html_table": TABLE_TEMPLATES,
    # CSV "Forward (Mesaj İletme) Akışı" → 'forward'; 'forward_chain' eski addır.
    "forward": FORWARD_CHAIN_TEMPLATES,
    "forward_chain": FORWARD_CHAIN_TEMPLATES,
    "calendar_invite": CALENDAR_INVITE_TEMPLATES,
    "i18n": I18N_TEMPLATES,
    "complex_html": COMPLEX_HTML_TEMPLATES,
}

# Gövdesi HTML olan senaryolar — sender bunları text/html olarak gönderir.
HTML_SCENARIOS = frozenset({"html_table", "complex_html"})


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


def resolve_html(html: str) -> str:
    """Resimsiz HTML şablonlarındaki {{SIGNATURE_HTML}} yer tutucusunu doldurur."""
    return html.replace("{{SIGNATURE_HTML}}", SIGNATURE_HTML)


_TAG_RE = re.compile(r"<[^>]+>")
_STYLE_RE = re.compile(r"<(style|script)[^>]*>.*?</\1>", re.S | re.I)
_BLOCK_BREAK_RE = re.compile(r"</(p|div|tr|h[1-6]|table)\s*>", re.I)


def html_to_plain(html: str) -> str:
    """HTML gövdeden okunabilir düz metin alternatifi üretir.

    multipart/alternative'in text/plain bacağı için kullanılır: HTML'i
    render edemeyen (ya da engelleyen) istemcide mesaj yine okunabilmelidir.
    """
    text = _STYLE_RE.sub("", html)
    text = _BLOCK_BREAK_RE.sub("\n", text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = _TAG_RE.sub("", text)
    text = unescape(text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()
