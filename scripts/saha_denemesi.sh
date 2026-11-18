#!/usr/bin/env bash
# =====================================================================
# saha_denemesi.sh — İlk saha denemesi: kurulum + tüm denemeler tek seferde
#
#   1. Python sanal ortamı ve bağımlılıklar
#   2. config.yaml (yoksa soru-cevapla oluşturulur)
#   3. Bağlantı testi (SMTP, IMAP, POP3, Z-Push) — mail göndermez
#   4. Z-Push teşhisi (adım adım)
#   5. EMS → EMS duman koşusu (referans olarak işaretlenir)
#   6. EMS → EMS kutu işlemleri (okundu/bayrak eşitlemesi, taşıma, silme, taslak)
#
# Adımlardan biri başarısız olsa da diğerleri çalışır. Tüm çıktı tek dosyada
# toplanır: saha_sonuc_<tarih>.txt — bu dosyayı paylaşın.
#
# Kullanım:  bash scripts/saha_denemesi.sh ["EMS sürüm etiketi"]
# =====================================================================
set -u
cd "$(dirname "$0")/.."

LABEL="${1:-}"
STAMP="$(date +%Y%m%d_%H%M%S)"
OUT="saha_sonuc_${STAMP}.txt"
PY="${PYTHON:-python3}"
declare -a SUMMARY=()

say() { printf '\n\033[1;36m══ %s ══\033[0m\n' "$*"; printf '\n══ %s ══\n' "$*" >> "$OUT"; }

# Çıktıyı hem ekrana hem sonuç dosyasına yazar; çıkış kodunu özete ekler.
step() {
  local title="$1"; shift
  say "$title"
  echo "\$ $*" >> "$OUT"
  "$@" 2>&1 | tee -a "$OUT"
  local code=${PIPESTATUS[0]}
  case $code in
    0) SUMMARY+=("✅ $title") ;;
    2) SUMMARY+=("⚠️  $title — başarısız kontroller var (kod 2)") ;;
    *) SUMMARY+=("❌ $title — hata (kod $code)") ;;
  esac
  return 0
}

{
  echo "Saha denemesi — $(date '+%Y-%m-%d %H:%M:%S')"
  echo "Dal: $(git rev-parse --abbrev-ref HEAD 2>/dev/null) @ $(git rev-parse --short HEAD 2>/dev/null)"
  echo "Sistem: $(uname -sr 2>/dev/null) | $($PY --version 2>&1)"
  echo "Etiket: ${LABEL:-—}"
} > "$OUT"

# ── 1. Python ortamı ─────────────────────────────────────────────────
say "1/6 Python ortamı"
if ! $PY -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
  echo "HATA: Python 3.10 veya üstü gerekli (bulunan: $($PY --version 2>&1)). PYTHON=python3.12 ile seçebilirsiniz." | tee -a "$OUT"
  exit 1
fi
if [ ! -d .venv ]; then
  $PY -m venv .venv || { echo "HATA: sanal ortam oluşturulamadı (Debian/Ubuntu: sudo apt install python3-venv)"; exit 1; }
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -q --upgrade pip >/dev/null 2>&1
if python -m pip install -q -r requirements.txt 2>&1 | tee -a "$OUT"; then
  SUMMARY+=("✅ Kurulum")
else
  echo "HATA: bağımlılıklar kurulamadı (kurum vekil sunucusu için: export HTTPS_PROXY=...)" | tee -a "$OUT"
  exit 1
fi

# ── 2. Ayarlar ───────────────────────────────────────────────────────
say "2/6 Ayarlar (config.yaml)"
if [ ! -f config.yaml ]; then
  python scripts/saha_kurulum.py || exit 1
else
  echo "config.yaml var, kullanılıyor (baştan sormak için: python scripts/saha_kurulum.py --yeniden)"
fi
# Şifrede görünmez karakter (ör. ok tuşu) varsa bağlanmadan önce yeniden sor
if ! python -c "
import sys, yaml
from mail_transport import credential_problem
cfg = yaml.safe_load(open('config.yaml', encoding='utf-8')) or {}
problems = [f'{n}: {p}' for n in ('ems', 'gmail') for p in [credential_problem(cfg.get(n) or {})] if p]
print('\n'.join(problems)); sys.exit(1 if problems else 0)
"; then
  echo "Şifre yeniden soruluyor..."
  python scripts/saha_kurulum.py --sifre || exit 1
fi
# Sonuç dosyasına ayarların ŞİFRESİZ özeti
python - >> "$OUT" 2>&1 <<'EOF'
import yaml
cfg = yaml.safe_load(open("config.yaml", encoding="utf-8")) or {}
for name in ("ems", "gmail"):
    s = cfg.get(name) or {}
    print(f"{name}: smtp={s.get('smtp_host')}:{s.get('smtp_port')} imap={s.get('imap_host')}:{s.get('imap_port')} "
          f"pop={s.get('pop_host') or s.get('imap_host')}:{s.get('pop_port') or 995} "
          f"eas={'var' if s.get('eas_url') else 'yok'} tls_verify={s.get('tls_verify', True)} "
          f"ca={'var' if s.get('tls_ca_file') else 'yok'} şifre={'var' if s.get('password') else 'YOK'}")
print("analiz:", (cfg.get("analysis") or {}).get("provider"), "| plan:", (cfg.get("test") or {}).get("plan"))
EOF
HAS_EAS=$(python -c "import yaml;print(1 if (yaml.safe_load(open('config.yaml',encoding='utf-8')).get('ems') or {}).get('eas_url') else 0)")
LABEL_ARGS=()
[ -n "$LABEL" ] && LABEL_ARGS=(--label "$LABEL")

# Test planında 0. yol: EMS → EMS (kurum içi). Gmail gerektirmez.
COMMON=(--config config.yaml --plan test_plan.yaml --combo 0)

# ── 3. Bağlantı testi ────────────────────────────────────────────────
step "3/6 Bağlantı testi (SMTP + IMAP + POP3 + Z-Push, mail gönderilmez)" \
  python main.py "${COMMON[@]}" --tier tam --dry-run

# ── 4. Z-Push teşhisi ────────────────────────────────────────────────
if [ "$HAS_EAS" = "1" ]; then
  say "4/6 Z-Push teşhisi"
  echo "\$ python scripts/zpush_check.py" >> "$OUT"
  python scripts/zpush_check.py 2>&1 | tee -a "$OUT"
  if [ "${PIPESTATUS[0]}" = "0" ]; then
    SUMMARY+=("✅ 4/6 Z-Push teşhisi")
  else
    SUMMARY+=("❌ 4/6 Z-Push teşhisi — ayrıntılı çıktı (--debug) sonuç dosyasında")
    say "4/6 Z-Push teşhisi — ayrıntılı (--debug)"
    python scripts/zpush_check.py --debug >> "$OUT" 2>&1
  fi
else
  SUMMARY+=("⏭  4/6 Z-Push teşhisi — eas_url tanımlı değil, atlandı")
fi

# ── 5. Duman koşusu ──────────────────────────────────────────────────
step "5/6 EMS → EMS duman koşusu" \
  python main.py "${COMMON[@]}" --tier duman "${LABEL_ARGS[@]}"
# Yalnızca temiz koşu referans olur: sonraki sürümler bununla karşılaştırılır.
if [[ "${SUMMARY[${#SUMMARY[@]}-1]}" == ✅* ]]; then
  python -c "
import run_history
runs = run_history.list_runs('reports')
if runs and run_history.set_baseline('reports', runs[0]['run_id']):
    print('📌 Duman koşusu referans işaretlendi: ' + runs[0]['run_id'])
" 2>&1 | tee -a "$OUT"
else
  echo "Duman koşusunda başarısız kontrol var — referans İŞARETLENMEDİ. Sonuçları inceleyip" \
       "arayüzde (Geçmiş & Fark) uygun koşuyu referans yapın." | tee -a "$OUT"
fi

# ── 6. Kutu işlemleri ────────────────────────────────────────────────
step "6/6 EMS → EMS kutu işlemleri" \
  python main.py "${COMMON[@]}" --tier tam --scenario group:kutu "${LABEL_ARGS[@]}"

# ── Özet ─────────────────────────────────────────────────────────────
say "ÖZET"
for line in "${SUMMARY[@]}"; do echo "$line" | tee -a "$OUT"; done
LATEST=$(ls -1d reports/runs/*/ 2>/dev/null | tail -2 | tr '\n' ' ')
{
  echo
  echo "Koşu dizinleri: ${LATEST:-—}"
  echo "Son raporlar: reports/test_report.html, reports/test_results.csv"
} | tee -a "$OUT"
printf '\n\033[1;32mBitti.\033[0m Paylaşılacak dosya: %s\n' "$OUT"
echo "Not: dosyada konu satırları ve test adresleri var; şifre yok. Paylaşmadan önce göz atın."
echo "Sonuçları arayüzde görmek için:  source .venv/bin/activate && python app.py"
