# Czwarte review planu implementacji esp32tl

Przegląd `docs/implementation-plan.md` (611 linii, zapis z 2026-10-29 21:27) po trzech
poprzednich review. Sprawdzony tego samego dnia względem issue #1 (bez zmian), `mikroverk/esp32sim`
na `2e36224` (#325 nadal otwarte, bez komentarzy) i `espressif/pytest-embedded` (main, PyPI 2.9.3).

## Werdykt

Rewizja zamyka wszystkie ustalenia z trzeciego review zmianami w treści; każde sprawdziłem
(sekcja 1). Nowe twierdzenia rewizji zgadzają się ze źródłami (sekcja 2).

Czwarte review poszło w dwa miejsca, których żadne poprzednie nie dotknęło: co pytest-embedded
naprawdę daje backendowi `serial` w M4 i jak faza PPP w M6 ma się do kontraktu czasu. Wyszły z
tego dwa ustalenia średnie i trzy drobne. Żadne nie blokuje M0 ani M1; oba średnie dotyczą M4
i M6 i można je wprowadzić przy pisaniu `docs/protocol.md`.

## 1. Zamknięcie ustaleń z trzeciego review

| Ustalenie | Gdzie zamknięte | Sprawdzone |
|---|---|---|
| 3.1 `embedded_services` nie przyjmie `esp32sim` | Sekcja 3 "Fixtures sit on pytest-embedded": brak usług + `msg_queue` dla `esp32sim`, `esp` dla `serial`, nic na CLI, `app_path` przez parametryzację | tak; `app_path` czyta `request.param` (`plugin.py:873-876`) |
| 3.2 "At once" vs reguła 2, `quiet_until` z odpowiedzi oczekujących | 4.1 reguła 1 (ostatnie zdanie) i reguła 2 ("At once" = `t_req + latency`), 4.4 reguła 2 i akapit o `quiet_until`, testy w M1 i M2 | tak |
| 3.3 Kanał diagnostyczny | `warn` i `stats` *ext* w 4.1, semantyka `stop`, M1 i M2 | tak |
| 3.4 Scenariusz a `quiet_until`, GPIO 12/15, `stop` | Akapit pod tabelą 4.1, M3, 4.5 | tak |

Deklaracja z sekcji 10 planu zgadza się ze stanem tekstu.

## 2. Weryfikacja nowych twierdzeń rewizji

| Twierdzenie | Źródło | Wynik |
|---|---|---|
| `app_path` parametryzowalne tak samo jak `embedded_services` | `plugin.py:866-876` | zgadza się |
| `msg_queue` to fixture, `DuplicateStdoutPopen(msg_queue, cmd, meta)` | `plugin.py:797`, `log.py` | zgadza się |
| `machine.script` publiczne, nadaje się na kolejkę `adc` | `esp-soc/src/machine.rs:40,89`: `pub script: Script { pub events: Vec<(u64, ScriptAction)>, pub pos: usize, ... }` | zgadza się; patrz 3.4 |
| GPIO 12 = VDD_SDIO, GPIO 15 = wyciszenie logu ROM, 5+15 = timing SDIO | strapping ESP32 | zgadza się |
| `--realtime` da się włączyć z biblioteki (M5) | `pub rt: Realtime` (`machine.rs:101`), CLI robi `m.rt.enabled = true` (`cli/src/lib.rs:829`) | zgadza się (plan tego nie cytuje; warto dopisać) |

## 3. Nowe ustalenia

### 3.1 Usługa `esp` nie wgrywa firmware'u; M4 potrzebuje `pytest-embedded-idf` (średnie)

Plan daje backendowi `serial` usługę `esp` i zależności `pytest-embedded-serial` oraz
`pytest-embedded-serial-esp`. `EspSerial` z tego pakietu ma tylko `hard_reset` i `erase_flash`
(`pytest_embedded_serial_esp/serial.py:158,163`). `flash()` żyje w `IdfSerial` w pakiecie
`pytest-embedded-idf` (`pytest_embedded_idf/serial.py:141`), razem z `IdfApp`, które czyta
`flasher_args.json` i zna offsety bootloadera, tablicy partycji i aplikacji
(`pytest_embedded_idf/app.py:47,87`).

Skutki:

- M4 ("`hello_world` i `nmea_reader` run on a real classic devkit") nie ma jak wgrać obrazu z
  artefaktu CI; test ruszałby na tym, co akurat siedzi na płytce, co jest sprzeczne z "every test
  that a real board can run, runs on one too" i z `app_sha256` w nagraniach.
- Backend `esp32sim` i tak potrzebuje offsetów do `--flash-at`; dziś plan nie mówi, skąd je
  bierze. `IdfApp(app_path)` daje je bez żadnej usługi (to zwykła klasa).

**Poprawka.** Zależności: dodać `pytest-embedded-idf` (2.9.3, zależy od `pytest-embedded` i
`esp-idf-panic-decoder`; extra `serial` ciągnie `pytest-embedded-serial-esp`). Backend `serial`
= usługi `esp,idf`, a M4 mówi wprost, że harness wgrywa artefakt przed testem (`IdfSerial.flash`)
i że `app_sha256` nagrania jest porównywany z tym, co wgrano. Backend `esp32sim` buduje
`IdfApp(app_path)` bezpośrednio i z `flash_files` składa `--flash-at`. Dodatkowy zysk: `idf`
daje dekoder paniki, który przyda się przy asercjach na konsoli.

### 3.2 Faza PPP w M6 to źródło `real` w przebraniu (średnie)

Po `CONNECT` model oddaje strumień bajtów do `pppd` w kontenerze. Od tej chwili drugi koniec
łącza żyje w czasie ściennym: bajty od `pppd` nie mają emulowanego `t`, a reguła 2 (spóźnione `t`
= błąd) i reguła 1 (`latency` deklarowane z góry) nie mają do czego się odnieść. To dokładnie
problem, który M5 rozwiązuje dla odbiornika GNSS na porcie hosta, a M6 go nie wspomina. Bez tego
albo peer stempluje bajty `pppd` zgadywanym `t` (i prędzej czy później łamie regułę 2), albo
emulator biegnie 15-37 razy szybciej niż `pppd`, i timery LCP/TCP w firmware wygasają, zanim
kontener odpowie.

**Poprawka.** M6: faza danych PPP używa stemplowania z M5 i biegnie pod `--realtime`
(`m.rt.enabled`, publiczne); dialog AT przed `CONNECT` pozostaje w czasie emulowanym. Sekcja 6:
testy PPP są wykluczone z joba determinizmu (czas ścienny z definicji nie jest powtarzalny), a
nagranie z taka sesją ma `clock: wall` od momentu `CONNECT`, co 4.3 powinno dopuszczać (dziś
`clock` jest polem nagłówka, czyli jednym na plik; potrzebny albo podział nagrania na `CONNECT`,
albo zdarzenie zmiany zegara). Do tabeli ryzyk: "PPP pod `--realtime` jest wolniejsze i
niepowtarzalne; model AT i warunki łącza nadal testują się deterministycznie bez `pppd`".

### 3.3 Fixture nie parametryzuje innego fixture'a (drobne)

Sekcja 3: "`board` selects the backend by indirect parametrisation of `embedded_services` and
`app_path`". Fixture nie może sparametryzować innego fixture'a; robi to hook
`pytest_generate_tests` w pluginie esp32tl, jednym wywołaniem `metafunc.parametrize` z obiema
nazwami naraz (`indirect=True`, wspólne `ids`), żeby `embedded_services` i `app_path` szły w
parach, a `--esp32tl-backend` filtrował listę przed parametryzacją. Zmiana jednego zdania.

### 3.4 `Stop` ma więcej wariantów niż `SwReset` (drobne)

`Stop` leży w `esp-soc/src/soc.rs:9` (plan cytuje `esp32`, które go reeksportuje; w porządku).
Warianty: `MaxInsns`, `Halted`, `Breakpoint`, `Unimplemented`, `SwReset`, `Simcall`, `Ebreak`,
`Watch`, `Exceptions`, `CostModel`, `CostModelLifecycle`. `Halted` to osiągnięcie `max_cycles`
(`machine.rs:584`), czyli normalny koniec po `--max-seconds`, nie błąd. Pozostałe (poza `SwReset`)
to koniec biegu z przyczyną: 4.2 powinno mówić, że każdy z nich daje `stats`, nazwę wariantu na
stderr i niezerowy kod wyjścia, a `Unimplemented` i `Exceptions` dodatkowo trafiają do `warn`,
bo to one odróżniają "firmware się zawiesiło" od "emulator nie umie". Dla M9: głęboki sen bez
budzika kończy się dziś `Halted` po `max_cycles`, więc test `deep_sleep` bez łatki przejdzie
cicho zamiast paść; M9 powinien asercję na wybudzenie formułować tak, by `Halted` był porażką.

### 3.5 `machine.script` jako kolejka `adc` (drobne)

`Script.events` jest publicznym `Vec<(u64, ScriptAction)>` z kursorem `pos`; maszyna idzie po nim
od `pos` w górę, więc dopisane zdarzenie musi być późniejsze niż wszystko, co kursor już minął,
i wstawione w porządku cykli. Własna kolejka w runnerze jest prostsza i nie zależy od tego
niepisanego porządku. Plan daje obie opcje; polecam skreślić `machine.script`.

## 4. Co zrobić

1. 3.1 i 3.2 wprowadzić do planu teraz (łącznie około 15 linii: zależności, M4, M6, sekcja 6,
   4.3, tabela ryzyk). Nie blokują M0/M1, ale zmieniają listę zależności, którą M0 utrwala w
   `pyproject.toml`.
2. 3.3 do 3.5 przy pisaniu `docs/protocol.md` i runnera w M1.
3. Start M0.
