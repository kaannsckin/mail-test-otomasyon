from client_profiles import apply_client_profile
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

html_body = "<html><body>Selam üğşöç</body></html>"
msg = MIMEMultipart("alternative")
html_part = MIMEText(html_body, "html", "utf-8")
msg.attach(html_part)

print("=== BEFORE CLIENT PROFILE ===")
print([p["Content-Transfer-Encoding"] for p in msg.walk() if p.get_content_type()=="text/html"])
print(msg.as_string())

apply_client_profile(msg, "ios")

print("=== AFTER CLIENT PROFILE ===")
print([p["Content-Transfer-Encoding"] for p in msg.walk() if p.get_content_type()=="text/html"])
print(msg.as_string())

