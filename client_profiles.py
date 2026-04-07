import re
from email.message import Message

def apply_client_profile(msg: Message, client_name: str) -> Message:
    """
    Belirtilen istemci profiline göre Message nesnesinin MIME yapısında
    ve HTML içerisinde (Header, VML, vs.) spoofing yapar.
    """
    if not client_name:
        return msg
        
    c_name = client_name.lower().strip()
    
    if "ios" in c_name or "iphone" in c_name or "apple" in c_name or "z-push" in c_name or "eas" in c_name:
        if "z-push" in c_name or "eas" in c_name:
            _apply_zpush_ios(msg)
        else:
            _apply_ios(msg)
    elif "outlook" in c_name or "exchange" in c_name or "windows" in c_name:
        _apply_outlook(msg)
    elif "android" in c_name and "gmail" in c_name:
        _apply_android_gmail(msg)
    elif "gmail" in c_name:
        _apply_gmail(msg)
    elif "thunderbird" in c_name:
        _apply_thunderbird(msg)
    elif "web" in c_name or "kurumsal" in c_name or "bizim" in c_name:
        _apply_custom_web(msg)
        
    return msg

def _apply_ios(msg: Message):
    if "X-Mailer" in msg:
        del msg["X-Mailer"]
    msg["X-Mailer"] = "Apple Mail (2.3654.120.3)"
    
    # Apple Mail özel boundary prefix manipülasyonu (mail kütüphanesinin otomatik ürettiği boundary'i değiştiriyoruz)
    if msg.is_multipart():
        current_boundary = msg.get_boundary()
        if current_boundary and not current_boundary.startswith("Apple-Mail-"):
            new_boundary = f"Apple-Mail-{current_boundary}"
            msg.set_boundary(new_boundary)
            
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            html = part.get_payload(decode=True)
            if html:
                try:
                    html_str = html.decode("utf-8")
                    if '<meta name="viewport"' not in html_str:
                        meta = '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
                        html_str = html_str.replace("<html>", f"<html><head>{meta}</head>")
                    part.set_payload(html_str, "utf-8")
                except UnicodeDecodeError:
                    pass

def _apply_outlook(msg: Message):
    if "X-Mailer" in msg:
        del msg["X-Mailer"]
    msg["X-Mailer"] = "Microsoft Outlook 16.0"
    
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            html = part.get_payload(decode=True)
            if html:
                try:
                    html_str = html.decode("utf-8")
                    
                    if "<html" in html_str and "xmlns:o=" not in html_str:
                        # Gelişmiş XML/VML namespace
                        html_str = html_str.replace("<html", '<html xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:w="urn:schemas-microsoft-com:office:word" xmlns:m="http://schemas.microsoft.com/office/2004/12/omml" ')
                        
                    mso_block = "\n<!--[if gte mso 9]><xml>\n <o:OfficeDocumentSettings>\n  <o:AllowPNG/>\n  <o:PixelsPerInch>96</o:PixelsPerInch>\n </o:OfficeDocumentSettings>\n</xml><![endif]-->\n"
                    
                    if "<body" in html_str:
                        html_str = re.sub(r'(<body[^>]*>)', r'\1' + mso_block, html_str)
                    else:
                        html_str = mso_block + html_str
                        
                    # Outlook spesifik paragraf sonları (Word render)
                    html_str = html_str.replace("</p>", '<o:p></o:p></p>')
                    part.set_payload(html_str, "utf-8")
                except UnicodeDecodeError:
                    pass

def _apply_gmail(msg: Message):
    if "X-Mailer" in msg:
        del msg["X-Mailer"]
        
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            html = part.get_payload(decode=True)
            if html:
                try:
                    html_str = html.decode("utf-8")
                    if "<body" in html_str:
                        html_str = re.sub(r'(<body[^>]*>)', r'\1<div dir="ltr" class="gmail_default">', html_str)
                        html_str = html_str.replace("</body>", "</div></body>")
                    else:
                        html_str = f'<div dir="ltr" class="gmail_default">{html_str}</div>'
                        
                    part.set_payload(html_str, "utf-8")
                except UnicodeDecodeError:
                    pass

def _apply_android_gmail(msg: Message):
    if "X-Mailer" in msg:
        del msg["X-Mailer"]
    # Android cihazlar genelde X-Mailer yerine kendi Google User-Agent'larını sızdırabilir
    msg["User-Agent"] = "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/116.0.0.0 Mobile Safari/537.36"
    msg["X-Android-Mail"] = "Gmail/App"
    # Gmail mimarisi HTML wrapperını kullan
    _apply_gmail(msg)

def _apply_thunderbird(msg: Message):
    if "X-Mailer" in msg:
        del msg["X-Mailer"]
    msg["User-Agent"] = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:115.0) Gecko/20100101 Thunderbird/115.8.1"
    
    # Thunderbird spesifik Content-Type sarmalama eklenebilir
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            part.set_param("format", "flowed")

def _apply_custom_web(msg: Message):
    # Orijinal Header'ları Sil ve Kurumnet'e Çevir
    if "X-Mailer" in msg:
        del msg["X-Mailer"]
    msg["X-Mailer"] = "TUBITAK Kurumnet"
    msg["MMHS-Primary-Precedence"] = "PRIORITY"
    msg["X-Priority"] = "3"
    msg["X-Client-IP"] = "159.146.13.73"
    
    import time, re, os
    msg["X-Submission-Time"] = str(int(time.time() * 1000))
    msg["X-Subject-Info"] = '{"a":"TC","b":"47947586976"}'
    
    # Özel HTML İmza (Kurumsal Web İstemcimize ait spesifik bir CSS DOM yapısı)
    signature_html = """
<br /><br /><table style="border-collapse:collapse;border:none;outline:none;font-family:'myriad pro' , 'arial' , 'helvetica' , sans-serif;color:#333333;font-size:13px;line-height:1.4"><tbody><tr style="border:none;outline:none"><td style="vertical-align:middle;padding:0 16px 0 0;width:128px;border:none;outline:none">
           <img src="cid:tf7b6bcqzkyk@@SafirPosta" style="width:120px;height:120px;border:0" />
         </td><td style="vertical-align:middle;border:none;outline:none;padding-left:16px;border-left:2px solid #b8860b">
      <div style="font-family:'minion pro' , 'georgia' , 'times new roman' , 'times' , serif;font-size:16px;line-height:1.1;font-weight:700;color:#333333;margin:0 0 4px 0">mkaasec</div>
      <div style="font-family:'myriad pro' , 'arial' , 'helvetica' , sans-serif;font-size:13px;color:#6b7280;margin:0 0 8px 0">aday araştırmacı</div>
      <div style="font-family:'myriad pro' , 'arial' , 'helvetica' , sans-serif;font-size:14px;font-weight:400;color:#333333;margin:0 0 8px 0"></div>
      <div style="font-family:'myriad pro' , 'arial' , 'helvetica' , sans-serif;font-size:12px;color:#333333;margin:0 0 2px 0">Gebze/kocaeli</div>
      <div style="font-family:'myriad pro' , 'arial' , 'helvetica' , sans-serif;font-size:12px;color:#333333;margin:0 0 2px 0">+9005555555555</div>
      <div style="font-family:'myriad pro' , 'arial' , 'helvetica' , sans-serif;font-size:12px;color:#333333;margin:0 0 2px 0"><a style="color:#333333;text-decoration:none">deneme@amaçlı.com</a></div>
      <div style="font-family:'myriad pro' , 'arial' , 'helvetica' , sans-serif;font-size:12px;color:#333333;margin:0 0 2px 0"><a href="https://bilgem.tubitak.gov.tr/" style="color:#b8860b;text-decoration:underline;font-family:'myriad pro' , 'arial' , 'helvetica' , sans-serif" rel="nofollow">https://bilgem.tubitak.gov.tr/</a></div>
    </td></tr></tbody></table>
"""
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            html = part.get_payload(decode=True)
            if html:
                try:
                    html_str = html.decode("utf-8", errors="replace")
                    if "</body>" in html_str:
                        html_str = html_str.replace("</body>", f"{signature_html}\n</body>")
                    else:
                        html_str = f"{html_str}\n{signature_html}"
                    part.set_payload(html_str, "utf-8")
                except Exception:
                    pass

    # Logoyu attachment olarak mesaja embed et
    logo_path = "assets/safir_logo.png"
    if os.path.exists(logo_path):
        from email.mime.image import MIMEImage
        img_part = MIMEImage(open(logo_path, "rb").read())
        img_part.add_header("Content-ID", "<tf7b6bcqzkyk@@SafirPosta>")
        img_part.add_header("Content-Disposition", "inline; filename=signatureImage.png")
        msg.attach(img_part)

def _apply_zpush_ios(msg: Message):
    """Z-Push / Exchange ActiveSync (EAS) üzerinden gelen iOS mesajlarını simüle eder."""
    if "X-Mailer" in msg:
        del msg["X-Mailer"]
    msg["X-Mailer"] = "Z-Push/EAS Client (2.3.9)"
    msg["X-MS-ASE-Version"] = "14.1"
    
    if msg.is_multipart():
        current_boundary = msg.get_boundary()
        if current_boundary and not current_boundary.startswith("Z-Push-"):
            msg.set_boundary(f"Z-Push-{current_boundary}")
            
    # iOS EAS genelde metin kısmını 'base64' olarak değil 'quoted-printable' olarak gönderir
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            part.replace_header("Content-Transfer-Encoding", "quoted-printable")
