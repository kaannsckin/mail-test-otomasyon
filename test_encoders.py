from email.mime.text import MIMEText
from email import encoders

part = MIMEText("test", "html", "utf-8")
del part["Content-Transfer-Encoding"]
part.set_payload("<html><head><meta name=\"viewport\"></head><body>selam üğşöç</body></html>".encode("utf-8"))
encoders.encode_quopri(part)

print(part.as_string())
