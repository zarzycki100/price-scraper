# Monitor cen Media Expert i RTV Euro AGD

Automatyczny scraper cen produktów z mediaexpert.pl i euro.com.pl, uruchamiany co 15 minut
przez GitHub Actions, z historią zapisywaną do CSV i wizualizacją na
GitHub Pages.

## Jak uruchomić

1. **Stwórz nowe repozytorium na GitHub** i wgraj do niego wszystkie pliki
   z tego projektu (zachowując strukturę katalogów).

2. **Dodaj produkty do śledzenia** — edytuj `products.txt`, jeden URL
   produktu z mediaexpert.pl lub euro.com.pl na linię (sklep jest
   rozpoznawany po domenie).

3. **Włącz uprawnienia do zapisu dla Actions:**
   Settings → Actions → General → Workflow permissions →
   „Read and write permissions” → Save.
   (Bez tego workflow nie będzie mógł commitować `data/prices.csv`.)

4. **Włącz GitHub Pages:**
   Settings → Pages → Source: „Deploy from a branch” →
   Branch: `main`, folder: `/docs` → Save.
   Strona z wykresem będzie dostępna pod
   `https://<twoj-login>.github.io/<nazwa-repo>/`.

5. **Odpal workflow ręcznie pierwszy raz**, żeby sprawdzić, czy działa:
   zakładka „Actions” → „Scrape prices” → „Run workflow”.

6. Od tego momentu workflow uruchamia się automatycznie co 15 minut,
   dopisuje nowe wiersze do `data/prices.csv` i commituje je do repo.
   Strona na GitHub Pages odczytuje ten plik na żywo i ma dwa widoki:
   - **Produkt** — historia ceny regularnej i promocyjnej jednego produktu
     (lista pogrupowana po sklepach, z oznaczeniem sklepu i linkiem),
   - **Porównanie w sklepie** (`#porownanie` w adresie) — ceny wszystkich
     produktów z wybranego sklepu na jednym wykresie, z legendą-tabelą pod
     wykresem (nazwy z linkami, cena teraz / najniższa / najwyższa,
     ukrywanie pojedynczych linii).

## Dodawanie kolejnego sklepu

W `scraper.py` dopisz funkcję `parse_<sklep>(soup, html)` zwracającą
`title`, `part_no`, `price`, `sale_price`, `availability` i dodaj domenę do
słownika `SHOPS`. Na stronie warto dopisać domenę do `SHOP_BY_HOST`
w `docs/index.html` (używane tylko dla starych wierszy bez kolumny `shop`).

## Struktura plików

```
scraper.py                     - skrypt scrapujący (curl_cffi + BeautifulSoup)
products.txt                   - lista URL-i produktów do monitorowania
requirements.txt               - zależności Pythona
data/prices.csv                - historia cen (tworzona automatycznie)
docs/index.html                - strona z wykresem (Chart.js) dla GitHub Pages
.github/workflows/scrape.yml   - harmonogram GitHub Actions (co 15 min)
```

## Uwagi

- Harmonogram cron w GitHub Actions działa w **UTC** i nie jest gwarantowany
  co do minuty — przy dużym obciążeniu GitHub może przesunąć start o kilka minut.
- GitHub automatycznie **wyłącza scheduled workflows po ~60 dniach** bez
  żadnego commitu w repo — wystarczy wtedy zrobić dowolny commit, żeby je
  reaktywować.
- Media Expert stoi za Cloudflare, który zwykłe `requests` odrzuca kodem 403
  (`cf-mitigated: challenge`). Dlatego skrypt używa `curl_cffi`, który
  podszywa się pod przeglądarkę Chrome (odcisk TLS/HTTP2). Jeśli 403 wróci,
  można spróbować innej wartości `IMPERSONATE` w `scraper.py`
  (np. `"safari"`, `"firefox"`) albo zaktualizować `curl_cffi`.
- Kolumna `shop` w CSV została dodana później — przy pierwszym uruchomieniu
  scraper sam przepisze istniejący `data/prices.csv` do nowego układu kolumn,
  uzupełniając sklep na podstawie URL-a.
- Dane w CSV rosną w nieskończoność. Przy bardzo długim monitorowaniu warto
  rozważyć okresowe archiwizowanie starszych wpisów.
