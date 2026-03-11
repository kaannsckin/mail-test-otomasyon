#!/bin/bash
# ============================================================
# Mail Otomasyon — GitHub Repo Kurulum Scripti
# Çalıştır: bash github_setup.sh
# ============================================================

REPO_NAME="mail-test-otomasyon"
GITHUB_USERNAME="BURAYA_GITHUB_KULLANICI_ADINIZI_YAZIN"

echo "🚀 GitHub repo kurulumu başlıyor..."

# 1. Git başlat
git init
git add .

# 2. .gitignore oluştur (şifreler ve loglar hariç)
cat > .gitignore << 'EOF'
# Güvenlik — asla commit etme
config.yaml
certs/
*.pem
*.key

# Python
__pycache__/
*.pyc
*.pyo
.venv/
venv/
*.egg-info/

# Test çıktıları
reports/
logs/
test_files/

# IDE
.vscode/
.idea/
*.swp
EOF

git add .gitignore
git commit -m "feat: Mail Test Otomasyon v2.0 - ilk commit

- SMTP/IMAP tabanlı mail iletim testi
- Claude API ile MIME analizi
- Flask web arayüzü
- 2FA / TOTP desteği (EMS, Gmail, Outlook)
- 5 senaryo tipi: plain_text, attachment, inline_image, reply_chain
- HTML + CSV raporlama
- 10 sunucu kombinasyonu"

# 3. GitHub'da repo oluştur (gh CLI ile — brew install gh)
# Önce: gh auth login
gh repo create $REPO_NAME \
  --public \
  --description "Mail servis sürüm güncelleme test otomasyonu — Claude API + SMTP/IMAP + 2FA" \
  --push \
  --source .

echo ""
echo "✅ Repo oluşturuldu!"
echo "🔗 https://github.com/$GITHUB_USERNAME/$REPO_NAME"
