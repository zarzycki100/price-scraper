#!/usr/bin/env bash
# Lokalne uruchamianie scrapera z crona (zamiast GitHub Actions, ktorego
# adresy IP sklepy blokuja 403).
#
# Dziala na OSOBNYM klonie repo ($CLONE_DIR), zeby nigdy nie wypchnac
# niedokonczonych zmian z kopii roboczej. Klon uzywa HTTPS + tokenu z `gh`
# (klucz SSH z haslem nie zadziala z crona, bo nie ma tam ssh-agenta).
#
# Wpis w crontab (crontab -e):
#   */30 * * * * /sciezka/do/repo/scripts/cron_scrape.sh
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/zarzycki100/price-scraper.git}"
CLONE_DIR="${CLONE_DIR:-$HOME/.local/share/price-scraper-cron}"
LOG_DIR="${LOG_DIR:-$HOME/.local/state/price-scraper}"
LOG_FILE="$LOG_DIR/cron.log"
LOG_MAX_LINES=5000

# cron ma bardzo ubogi PATH
export PATH="/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin"

mkdir -p "$LOG_DIR"
exec >>"$LOG_FILE" 2>&1

# nie dopuszczamy do dwoch przebiegow naraz (np. gdy poprzedni sie zawiesil)
exec 9>"$LOG_DIR/cron.lock"
if ! flock -n 9; then
  echo "$(date -Is) poprzedni przebieg jeszcze trwa - pomijam"
  exit 0
fi

# losowe opoznienie startu (0-3 min) - zeby requesty nie przychodzily zawsze
# rowno o :00/:15/:30/:45, co jest typowym sladem crona
START_JITTER_MAX="${START_JITTER_MAX:-180}"
sleep $((RANDOM % (START_JITTER_MAX + 1)))

echo "===== $(date -Is) ====="

if [ ! -d "$CLONE_DIR/.git" ]; then
  git clone --quiet "$REPO_URL" "$CLONE_DIR"
fi
cd "$CLONE_DIR"
git config credential.helper '!gh auth git-credential'
git config user.name "price-scraper (cron)"
git config user.email "$(git -C "$CLONE_DIR" config --global user.email || echo cron@localhost)"

# klon sluzy tylko cronowi - zawsze dokladnie stan z GitHuba
git fetch --quiet origin main
git reset --quiet --hard origin/main

python3 scraper.py || status=$?

git add data/prices.csv
if git diff --cached --quiet; then
  echo "Brak zmian w danych."
else
  git commit --quiet -m "Aktualizacja cen $(date -u +'%Y-%m-%d %H:%M') UTC"
  # jesli w miedzyczasie ktos wypchnal zmiany - dociagamy i probujemy ponownie
  for attempt in 1 2 3; do
    if git push --quiet origin HEAD:main; then
      echo "Wypchnieto dane."
      break
    fi
    git pull --quiet --rebase origin main
  done
fi

# przycinanie logu do ostatnich LOG_MAX_LINES linii
tail -n "$LOG_MAX_LINES" "$LOG_FILE" >"$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"

exit "${status:-0}"
