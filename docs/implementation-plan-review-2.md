# Ponowne review planu implementacji esp32tl

Przegląd zrewidowanego `docs/implementation-plan.md` (547 linii, zapis z 2026-10-29 19:13) po
pierwszym review (`docs/implementation-plan-review.md`). Sprawdzony tego samego dnia względem
issue #1 (bez zmian od pierwszego review), `mikroverk/esp32sim` na `2e36224` i PyPI.

## Werdykt

Rewizja zamyka wszystkie 17 ustaleń z pierwszego review plus wszystkie drobne, i robi to
zmianami w treści, nie deklaracjami. Każde zamknięcie sprawdziłem (sekcja 1). Nowe twierdzenia
dodane w rewizji trzymają się źródeł z jednym wyjątkiem.

Plan nadaje się do startu M0. Przed startem trzeba poprawić dwie rzeczy w tekście:

1. **`pytest-embedded[serial,esp]` nie istnieje.** pytest-embedded 2.9.3 nie deklaruje żadnych
   extras. Usługi `serial` i `esp` to osobne pakiety: `pytest-embedded-serial` i
   `pytest-embedded-serial-esp` (`SERVICE_LIB_NAMES`, `pytest_embedded/utils.py:20-23`).
   `pip install` z obecnym `pyproject.toml` się nie powiedzie.
2. **Reguła 2 protokołu (spóźnione `t` to błąd) plus "timing is the recording's" (4.4) wymuszają
   związek między `latency` peera a nagraniem, którego plan nie zapisuje.** Peer odtwarzający
   nagranie z odpowiedzią po `d` ns, który zadeklarował `latency > d`, dostanie błąd protokołu
   przy pierwszym takim żądaniu. Trzeba zapisać, że peer `replay` wyznacza `latency` jako minimum
   opóźnień w swoim nagraniu, a `model` jako swoje najkrótsze możliwe opóźnienie. Do tego brakuje
   zdania o sklejaniu bajtów: `uart_tx` w trait'cie strzela per bajt, a wiadomość `uart_tx` niesie
   `data`; plan nie mówi, kiedy runner opróżnia bufor (najpóźniej w punkcie blokady i przy zmianie
   trasy). Bez tego "nothing in Python runs per byte" z ustalenia 8 jest intencją, nie kontraktem.

Reszta (sekcja 3) to doprecyzowania, które można wprowadzić w trakcie M0/M1.

## 1. Zamknięcie ustaleń z pierwszego review

| Ustalenie | Gdzie zamknięte | Sprawdzone |
|---|---|---|
| 1.1 M1 bez trasy GNSS w profilu | Ustalenie 12, profil `esp32-devkitc` (4.5), M1 | tak; GPIO 16/17 słusznie ominięte (PSRAM na WROVER) |
| 1.2 `real` bez kamienia milowego | Ustalenie 11, M5, tabela zdolności w 4.5 | tak |
| 1.3 ADC poza `BoardModel` | Ustalenie 10, wiadomości *ext* w 4.1, M3, M10, ryzyka | tak; `ScriptAction::Analog` istnieje (`esp-soc/src/machine.rs:17`), a nałożenie idzie przez `SocBus::analog_set` (`machine.rs:1058`, `esp32/src/bus.rs:1025`), które runner może wołać bezpośrednio |
| 1.4 Protokół: kwity, spóźnienia, `sync`, start | Reguły 1-4 w 4.1, test z fałszywym peerem w M1 | tak, z zastrzeżeniem 2 z werdyktu |
| 1.5 Docker na macOS | M0 (job `firmware` na ubuntu + artefakt), marker `requires_docker`, ryzyka | tak |
| 2.1 "(v0.3.0)" | Nagłówek: "227 commits after v0.3.0" | tak |
| 2.2 Baud na trasie tylko-odbiorczej | `uart_cfg` *ext* w 4.1, M1 | tak; patrz 3.1 niżej: decyzja "w M1" jest już rozstrzygalna |
| 2.3 Dystrybucja runnera | Sekcja 3 "Distribution", M10 (prebuilt) | tak |
| 2.4 Dwa słowniki zegara | `hello.clock` i nagłówek nagrania używają `emulated-fifo-write` / `wall` | tak |
| 2.5 `hello.board` | Harness dodaje `board` | tak |
| 2.6 M3 za szeroki | `serial` wydzielony do M4 | tak |
| 2.7 Fixture'y vs pytest-embedded | Sekcja 3 "Fixtures sit on pytest-embedded" | tak |
| 2.8 `StationLink` publiczny | M7 cytuje `cli/src/lib.rs:554`, `esp-soc/src/wifi.rs:490` | tak |
| 2.9 SPI jako ścieżka główna | M7, ryzyko zawężone do DMA | tak |
| 2.10 Python 3.14 | M0, sekcja 6 | tak |
| 2.11 Próg publikacji | Tabela decyzji, M1 "The repository goes public here" | tak, wrócono do decyzji specyfikacji |
| 2.12 `unknown_reply` per protokół | Tabela w 4.4 | tak, z dwiema poprawkami treści (3.4) |
| Drobne: właściciel konsoli, `reboot() -> u32`, ROM `20241011`, próg 5 s, `app_sha256` | Tabela decyzji, ustalenie 4, M0, M1, 4.3 | tak |

Deklaracja z sekcji 10 planu ("closed: 1.1 to 1.5, 2.1 to 2.12, all of section 3; answered
with no: none") zgadza się ze stanem tekstu.

## 2. Weryfikacja nowych twierdzeń rewizji

| Twierdzenie | Źródło | Wynik |
|---|---|---|
| `ScriptAction::Analog(pin, AnalogSource)` w `esp-soc/src/machine.rs` | linia 17; `analog_set` w 1058 | zgadza się |
| `reboot()` zwraca `u32` z przyczyną | `esp-soc/src/machine.rs:306` | zgadza się |
| `attach_wifi` wołane z `cli` na klasycznej maszynie | `cli/src/lib.rs:554-556` | zgadza się (linia 555, nie 554; nieistotne) |
| GPIO 16/17 zajęte przez PSRAM na WROVER | dokumentacja Espressif WROVER | zgadza się |
| `pytest-embedded[serial,esp]` jako zależność | PyPI `requires_dist`, `provides_extra` | **nie**: brak extras |
| Hostowane runnery macOS bez Dockera | GitHub Actions | zgadza się |
| `UBX-ACK-NAK` dla klasy i id żądania | protokół u-blox | zgadza się |
| `$PMTK001,<cmd>,0` = "command not supported" | spec MediaTek PMTK | **nieścisłe**: flaga 0 = invalid packet, 1 = unsupported (3.4) |

## 3. Nowe ustalenia (do wprowadzenia w trakcie M0/M1)

### 3.1 `uart_cfg`: decyzja "w M1" jest już do podjęcia

Plan zostawia na M1 pytanie, czy runner czyta rejestry UART, czy magistrala wystawia trasę.
Magistrala już wystawia: `bus.periph` jest `pub` (`esp32/src/bus.rs:60`), a
`periph.uart_route(port)` jest `pub` i zwraca `UartRoute { port, tx_pins, rx_pin, baud:
Option<u32> }` (`esp32/src/periph.rs:808`, `esp-soc/src/uart.rs:7-14`). Runner odpytuje trasę
w `advance_to` i wysyła `uart_cfg`, gdy wynik się zmieni. Jedyna reszta do zapisania: `baud` jest
`None`, dopóki firmware nie skonfiguruje dzielnika, więc `uart_cfg` z `baud: null` to prawidłowy
stan początkowy, a nie błąd.

### 3.2 M5: `--realtime` a kontrakt `wait`

Projekt M5 ma powstać przed kodem, ale lista jego punktów pomija to, co pierwszy review nazwał
ryzykiem zakleszczenia w nowej postaci. Prawdziwe urządzenie nie zna swojego `quiet_until`.
Źródło `real` musi więc odnawiać `wait` w krótkich plastrach (np. 1 ms emulowana), a runner pod
`--realtime` w punkcie blokady musi czekać na gniazdo z timeoutem do chwili, gdy czas ścienny
dogoni emulowany, zamiast blokować bez końca. Dopisać oba zdania do listy projektowej M5, żeby nie
wyszły dopiero w implementacji.

### 3.3 `--esp32tl-port` dubluje `--port` pytest-embedded

pytest-embedded ma już opcję `--port` dla usługi `serial`. Skoro M4 deleguje `serial` do
pytest-embedded, własna opcja to drugi sposób na to samo. Używać `--port`; zostawić tylko
`--esp32tl-backend`.

### 3.4 Dwie wartości w tabeli `Protocol` (4.4)

- PMTK: `$PMTK001,<cmd>,<flag>`: 0 = invalid packet, 1 = unsupported packet type, 2 = valid but
  failed, 3 = valid and succeeded. Dla nieznanego polecenia właściwa jest flaga 1, nie 0.
- AT: w trybie verbose (ATV1, domyślny) odpowiedź to `\r\nERROR\r\n`, nie `ERROR\r\n`. Firmware
  zwykle dopasowuje po `\r\nERROR`, więc brak wiodącego `\r\n` może nie trafić w parser.

### 3.5 Odwołania i spójność numeracji

- Nagłówek planu wskazuje `.context/attachments/DY4p2N/implementation-plan-review.md`. Katalog
  `.context/` jest poza gitem, więc po commicie to odwołanie wisi. Wskazać
  `docs/implementation-plan-review.md`.
- Ustalenie 12 i 4.5 mówią, że profil `esp32-devkitc` "carries milestones 0 to 4", a M6 i M7
  też na nim jadą (z dodaną trasą `[uart.modem]`). Napisać "carries every validation firmware";
  profil LilyGO nie jest potrzebny żadnemu kamieniowi.
- Ustalenie 5: "Milestones 0 to 7 pin upstream `main`" zgadza się z M8 (HCI może potrzebować
  gałęzi z łatkami) i M9 (pierwsze użycie gałęzi). Spójne.

### 3.6 Drobne

- GPIO 5 (RX trasy GNSS w profilu devkit) to pin strapping ESP32 (timing SDIO slave). Odbiornik
  trzymający linię nisko przy boocie nie zmienia bootu, ale warto to zapisać w komentarzu
  profilu, żeby nikt nie szukał przyczyny, gdy kiedyś zmieni się na GPIO 12 lub 15.
- 4.1 wiersz `uart_cfg`: pole `rx_pin` odpowiada `UartRoute.rx_pin: Option<u8>`, więc w JSON może
  być `null`. Zapisać jak dla `baud`.
- Sekcja 9 nadal planuje M0 + M1 w dwa tygodnie, teraz z dwujobowym CI i testem fałszywego peera
  pokrywającym reguły 2 i 4. Wykonalne, ale M0 urósł; jeśli coś ma wypaść z dwóch tygodni, to
  prebuilt firmware w CI (lokalny build Dockerem wystarcza na M1).

## 4. Co zrobić

1. Poprawić zależności w sekcji 3 i M0: `pytest-embedded`, `pytest-embedded-serial`,
   `pytest-embedded-serial-esp`.
2. Dopisać do 4.1 regułę o `latency` peera względem nagrania i o sklejaniu `uart_tx`; do 4.4
   jedno zdanie, że silnik dopasowań wylicza `latency` z nagrania.
3. 3.1 do 3.5 wprowadzić przy pisaniu `docs/protocol.md` w M1; żadne nie blokuje M0.
