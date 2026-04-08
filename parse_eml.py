import email
from email import policy

with open('/Users/kaan/Downloads/isim_degisikligi.eml', 'rb') as f:
    msg = email.message_from_binary_file(f, policy=policy.default)

for part in msg.walk():
    if part.get_filename() == 'signatureImage.png':
        with open('assets/safir_logo.png', 'wb') as out:
            out.write(part.get_payload(decode=True))
        print("Logo saved to assets/safir_logo.png")
