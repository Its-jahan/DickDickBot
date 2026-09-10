#!/usr/bin/env bash
#
# One-shot setup for the Mini App service (dickbot-web) on the production server.
# Run as root ON THE SERVER, from the repo checkout:
#
#     sudo bash /opt/DickDickBot/deploy/setup-web.sh
#
# Idempotent: safe to re-run after a config change or a failed attempt. It installs the
# systemd unit, installs the nginx vhost, obtains a TLS certificate, and starts the
# service - then proves the whole chain works by fetching /healthz through nginx.
#
# WHAT IT CANNOT DO, AND WHY
# --------------------------
#   * The DNS record. That lives in Cloudflare and needs an API token this script has
#     no business holding. It checks the name resolves and stops with instructions if
#     it doesn't, rather than letting certbot fail with a confusing error.
#   * BotFather /setdomain. There is no API for it - it is a chat with BotFather. Only
#     the LOGIN WIDGET needs it (playing in an ordinary browser); the Mini App opened
#     from /app inside Telegram works without it. The script prints the exact steps at
#     the end.
set -euo pipefail

DOMAIN="${DOMAIN:-app.inddex.app}"
REPO="${REPO:-/opt/DickDickBot}"
PORT="${PORT:-8012}"
UNIT=dickbot-web
ACME_ROOT=/var/www/acme-challenge
EMAIL="${CERTBOT_EMAIL:-}"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m !  %s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31m ✗  %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "این اسکریپت باید با root اجرا بشه:  sudo bash $0"

# ---------------------------------------------------------------- preflight
say "بررسی پیش‌نیازها"
[ -d "$REPO/python_bot" ] || die "ریپو توی $REPO پیدا نشد. با REPO=/path/to/repo اجراش کن."
[ -x "$REPO/venv/bin/gunicorn" ] || die "gunicorn توی $REPO/venv نیست. اول: $REPO/venv/bin/pip install -r $REPO/python_bot/requirements.txt"
[ -f "$REPO/python_bot/webapp.py" ] || die "webapp.py پیدا نشد — اول git pull کن."
[ -f /etc/dickbot-admin.env ] || die "/etc/dickbot-admin.env نیست. سرویس وب SUPABASE_DB_URL رو از همین فایل می‌خونه."
command -v nginx >/dev/null || die "nginx نصب نیست."

if ! getent hosts "$DOMAIN" >/dev/null; then
  die "دامنهٔ $DOMAIN رزولو نمی‌شه.
      اول توی Cloudflare یک رکورد A بساز که به IP همین سرور اشاره کنه (پروکسی روشن هم اوکیه)،
      چند دقیقه صبر کن، بعد دوباره این اسکریپت رو بزن."
fi
echo "    $DOMAIN -> $(getent hosts "$DOMAIN" | awk '{print $1}' | tr '\n' ' ')"

# ---------------------------------------------------------------- 1. systemd
say "۱/۴ نصب سرویس $UNIT"
install -m 0644 "$REPO/deploy/$UNIT.service" "/etc/systemd/system/$UNIT.service"
# The shipped unit points at /opt/DickDickBot; honour a REPO override.
if [ "$REPO" != "/opt/DickDickBot" ]; then
  sed -i "s#/opt/DickDickBot#$REPO#g" "/etc/systemd/system/$UNIT.service"
fi
systemctl daemon-reload
systemctl enable "$UNIT" >/dev/null
systemctl restart "$UNIT"
sleep 2
systemctl is-active --quiet "$UNIT" || {
  journalctl -u "$UNIT" -n 40 --no-pager || true
  die "سرویس بالا نیومد — لاگ بالا رو ببین."
}
# Prove it is actually serving, not merely 'active'.
curl -fsS --max-time 5 "http://127.0.0.1:$PORT/healthz" >/dev/null \
  || { journalctl -u "$UNIT" -n 40 --no-pager || true; die "سرویس جواب /healthz نمی‌ده."; }
echo "    ✓ روی 127.0.0.1:$PORT جواب می‌ده"

# ---------------------------------------------------------------- 2. nginx (HTTP first)
say "۲/۴ نصب کانفیگ nginx"
mkdir -p "$ACME_ROOT"
SITE=/etc/nginx/sites-available/$DOMAIN
install -m 0644 "$REPO/deploy/nginx-app.inddex.app.conf" "$SITE"
if [ "$DOMAIN" != "app.inddex.app" ]; then
  sed -i "s/app\.inddex\.app/$DOMAIN/g" "$SITE"
fi
ln -sfn "$SITE" "/etc/nginx/sites-enabled/$DOMAIN"

# certbot needs port 80 reachable BEFORE the 443 block can reference a cert that does
# not exist yet - nginx refuses to start with a missing ssl_certificate. So serve only
# the HTTP half until the cert is in place.
CERT=/etc/letsencrypt/live/$DOMAIN/fullchain.pem
if [ ! -f "$CERT" ]; then
  warn "هنوز گواهی نداریم — موقتاً فقط بلاک HTTP رو فعال می‌کنم تا certbot بتونه کار کنه"
  awk '/^server \{/{n++} n==1' "$REPO/deploy/nginx-app.inddex.app.conf" \
    | sed "s/app\.inddex\.app/$DOMAIN/g" > "$SITE"
fi
nginx -t || die "کانفیگ nginx مشکل داره."
systemctl reload nginx

# ---------------------------------------------------------------- 3. certificate
say "۳/۴ گرفتن گواهی TLS برای $DOMAIN"
if [ -f "$CERT" ]; then
  echo "    ✓ از قبل وجود داره — رد شد"
else
  command -v certbot >/dev/null || die "certbot نصب نیست:  apt-get install -y certbot"
  ARGS=(certonly --webroot -w "$ACME_ROOT" -d "$DOMAIN" --non-interactive --agree-tos)
  if [ -n "$EMAIL" ]; then ARGS+=(-m "$EMAIL"); else ARGS+=(--register-unsafely-without-email); fi
  certbot "${ARGS[@]}" || die "certbot نتونست گواهی بگیره.
      معمول‌ترین دلیل: Cloudflare پورت ۸۰ رو به سرور پاس نمی‌ده، یا رکورد DNS هنوز پخش نشده.
      چک کن:  curl -I http://$DOMAIN/.well-known/acme-challenge/test"
  # Now the full vhost (HTTP redirect + HTTPS) can be installed.
  install -m 0644 "$REPO/deploy/nginx-app.inddex.app.conf" "$SITE"
  # Spelled as an `if` rather than `[ ... ] && sed` purely for symmetry with the two
  # other substitutions above. (`set -e` does not fire on either form here: it ignores
  # a non-final command in an AND-OR list.)
  if [ "$DOMAIN" != "app.inddex.app" ]; then
    sed -i "s/app\.inddex\.app/$DOMAIN/g" "$SITE"
  fi
  nginx -t || die "کانفیگ کامل nginx مشکل داره."
  systemctl reload nginx
  echo "    ✓ گواهی گرفته شد"
fi

# ---------------------------------------------------------------- 4. end to end
say "۴/۴ تست سرتاسری"
if curl -fsS --max-time 10 "https://$DOMAIN/healthz" | grep -q '"ok"'; then
  echo "    ✓ https://$DOMAIN/healthz جواب داد"
else
  warn "از بیرون جواب نداد. اگه Cloudflare جلوشه، حالت SSL باید Full (strict) باشه؛"
  warn "گواهی self-signed خطای 526 می‌ده. لوکال سالمه، پس مشکل بین CF و اوریجینه."
fi

cat <<EOF

──────────────────────────────────────────────────────────────
✅ سرویس وب راه افتاد.

  سرویس:   systemctl status $UNIT
  لاگ:     journalctl -u $UNIT -f
  آدرس:    https://$DOMAIN/

الان همین‌جوری کار می‌کنه:
  • توی گروه /app بزن → دکمهٔ «باز کردن بازی» → اپ باز می‌شه.
    (برای این هیچ تنظیم دیگه‌ای لازم نیست.)

یک کار دستی مونده، فقط برای «ورود با تلگرام» توی مرورگر معمولی:
  1. توی تلگرام برو سراغ @BotFather
  2. /setdomain  →  بات رو انتخاب کن  →  بفرست:  $DOMAIN
  بدون این، دکمهٔ لاگین توی مرورگر کار نمی‌کنه (ولی /app سالمه).
──────────────────────────────────────────────────────────────
EOF
