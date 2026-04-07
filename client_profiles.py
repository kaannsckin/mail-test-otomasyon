import re
from email.message import Message

def apply_client_profile(msg: Message, client_name: str) -> Message:
    """
    Belirtilen istemci profiline göre Message nesnesinin MIME yapısında
    ve HTML içerisinde (Header, VML, <o:p>, <div dir="ltr"> vs.)
    spoofing/taklit yapar.
    """
    if not client_name:
        return msg
        
    c_name = client_name.lower().strip()
    
    if "ios" in c_name or "iphone" in c_name or "apple" in c_name:
        _apply_ios(msg)
    elif "outlook" in c_name or "exchange" in c_name:
        _apply_outlook(msg)
    elif "gmail" in c_name:
        _apply_gmail(msg)
        
    return msg

def _apply_ios(msg: Message):
    if "X-Mailer" in msg:
        del msg["X-Mailer"]
    msg["X-Mailer"] = "iPhone Mail (20D47)"
    
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
                        html_str = html_str.replace("<html", '<html xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:w="urn:schemas-microsoft-com:office:word" ')
                        
                    mso_block = "\n<!--[if gte mso 9]><xml>\n <o:OfficeDocumentSettings>\n  <o:AllowPNG/>\n  <o:PixelsPerInch>96</o:PixelsPerInch>\n </o:OfficeDocumentSettings>\n</xml><![endif]-->\n"
                    
                    if "<body" in html_str:
                        html_str = re.sub(r'(<body[^>]*>)', r'\1' + mso_block, html_str)
                    else:
                        html_str = mso_block + html_str
                        
                    html_str = html_str.replace("</p>", "<o:p></o:p></p>")
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
