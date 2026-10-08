#!/usr/bin/env bash
# Lokalne uruchamianie scrapera z crona (zamiast GitHub Actions, ktorego
# adresy IP sklepy blokuja 403).
#
# Dziala na OSOBNYM klonie repo ($CLONE_DIR), zeby nigdy nie wypchnac
# niedokonczonych zmian z kopii roboczej. Klon uzywa HTTPS + tokenu z `gh`
# (klucz SSH z haslem nie zadziala z crona, bo nie ma tam ssh-agenta).
#
# Tryby (argument):
#   ceny        (domyslny) zgloszenia dodania produktow + pobranie cen
#   zgloszenia  tylko zgloszenia z formularza docs/dodaj.html (add_products.py) -
#               czesto, zeby wyniki wyszukiwania i dodanie produktu nie czekaly
#               na przebieg cen; bez zgloszen nie uruchamia przegladarki i nic nie loguje
#
# Wpisy w crontab (crontab -e):
#   7,37 * * * * /sciezka/do/repo/scripts/cron_scrape.sh
#   */5 * * * *  /sciezka/do/repo/scripts/cron_scrape.sh zgloszenia
set -euo pipefail

# Cala logika w funkcji: bash wczytuje ja w calosci przed uruchomieniem. Skrypt
# odpalany jest z kopii roboczej, a bash czyta zwykly skrypt kawalkami w trakcie
# dzialania - edycja pliku w czasie przebiegu (git pull, edytor) potrafila go
# wywrocic przed commitem danych.
main() {
  MODE="${1:-ceny}"

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
    # zgloszenia sprawdzamy co 5 min - nie zasmiecamy logu, gdy trwa przebieg cen
    [ "$MODE" = ceny ] && echo "$(date -Is) poprzedni przebieg jeszcze trwa - pomijam"
    exit 0
  fi

  if [ "$MODE" = ceny ]; then
    # losowe opoznienie startu (0-2 min) - zeby requesty nie przychodzily zawsze
    # o tej samej minucie, co jest typowym sladem crona
    START_JITTER_MAX="${START_JITTER_MAX:-120}"
    sleep $((RANDOM % (START_JITTER_MAX + 1)))
    echo "===== $(date -Is) ====="
  fi

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

  if [ "$MODE" = zgloszenia ]; then
    out=$(python3 add_products.py 2>&1) || out="$out
  add_products.py zakonczyl sie bledem"
    [ -z "$out" ] && exit 0  # brak zgloszen
    echo "===== $(date -Is) (zgloszenia) ====="
    echo "$out"
  else
    # zgloszenia "dodaj-produkt" przed scraperem, zeby dodane produkty mialy ceny
    # juz w tym przebiegu; blad nie blokuje scrapowania
    python3 add_products.py || echo "add_products.py zakonczyl sie bledem"
    python3 scraper.py || status=$?
  fi

  git add data/prices.csv products.csv
  if git diff --cached --quiet; then
    echo "Brak zmian w danych."
  else
    msg="Aktualizacja cen $(date -u +'%Y-%m-%d %H:%M') UTC"
    if ! git diff --cached --quiet -- products.csv; then
      msg="Nowe produkty i aktualizacja cen $(date -u +'%Y-%m-%d %H:%M') UTC"
    fi
    git commit --quiet -m "$msg"
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
}

main "$@"
exit
