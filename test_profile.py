from client_profiles import apply_client_profile
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

m = MIMEMultipart()
html = MIMEText("<html><body>Selam üğşöç</body></html>", "html", "utf-8")
m.attach(html)
apply_client_profile(m, "ios")
print(m.as_string())
