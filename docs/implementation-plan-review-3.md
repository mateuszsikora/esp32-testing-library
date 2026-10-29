# Trzecie review planu implementacji esp32tl

Przegląd `docs/implementation-plan.md` (574 linie, zapis z 2026-10-29 19:42) po dwóch
poprzednich review. Sprawdzony tego samego dnia względem issue #1 (bez zmian), `mikroverk/esp32sim`
na `2e36224` (#325 nadal otwarte, bez komentarzy) i `espressif/pytest-embedded` (main, PyPI 2.9.3).

## Werdykt

Rewizja zamyka oba ustalenia blokujące z drugiego review i wszystkie z sekcji 3, zmianami w
treści. Każde zamknięcie sprawdziłem (sekcja 1). Nowe twierdzenia rewizji zgadzają się ze
źródłami (sekcja 2).

Plan nie ma już ustaleń blokujących. Trzecie review poszło tam, gdzie poprzednie nie zaglądały:
w szwy między kontraktami (4.1 z 4.4) i w to, jak fixture'y naprawdę siedzą na pytest-embedded.
Wyszły z tego trzy ustalenia średnie, do wprowadzenia w trakcie M0/M1, i trzy drobne. Żadne nie
zatrzymuje startu.

## 1. Zamknięcie ustaleń z drugiego review

| Ustalenie | Gdzie zamknięte | Sprawdzone |
|---|---|---|
| Blok 1: `pytest-embedded[serial,esp]` nie istnieje | Architektura (`pyproject.toml`), "Distribution" | tak; `pytest-embedded-serial` 2.9.3 i `pytest-embedded-serial-esp` 2.9.3 są na PyPI, zależności zgodne z planem |
| Blok 2: `latency` a nagranie, sklejanie `uart_tx` | 4.1 reguła 2 (ostatnie zdania), wiersz `uart_tx`, 4.4 akapit o `latency`, M1 | tak, z zastrzeżeniem 3.2 niżej |
| 3.1 `uart_cfg` z `bus.periph.uart_route` | Wiersz `uart_cfg`, M1 | tak, `null` dla `baud` i `rx_pin` zapisane |
| 3.2 M5: `wait` w plastrach, timeout pod `--realtime` | M5 | tak |
| 3.3 `--esp32tl-port` | Architektura, M4: `--port` pytest-embedded | tak; `--port` jest w bazowym pluginie (`plugin.py:167`) |
| 3.4 PMTK flaga 1, AT `\r\nERROR\r\n` | Tabela w 4.4 | tak |
| 3.5 Odwołania i numeracja | Nagłówek, ustalenie 12, 4.5 | tak |
| 3.6 GPIO 5, `rx_pin: null`, sekcja 9 | 4.5, wiersz `uart_cfg`, sekcja 9 | tak |

Deklaracja z sekcji 10 planu zgadza się ze stanem tekstu.

## 2. Weryfikacja nowych twierdzeń rewizji

| Twierdzenie | Źródło | Wynik |
|---|---|---|
| pytest-embedded 2.9.3 `requires_dist`: pytest, pexpect, filelock; bez extras | PyPI | zgadza się |
| `pytest-embedded-serial`, `pytest-embedded-serial-esp` jako osobne zależności | PyPI, `SERVICE_LIB_NAMES` | zgadza się |
| `--port` należy do pytest-embedded | `pytest_embedded/plugin.py:167` | zgadza się |
| `SocBus::analog_set` wołane przez `ScriptAction::Analog` | `esp-soc/src/machine.rs:1058` | zgadza się |
| `bus.periph.uart_route(port)` publiczne | `esp32/src/bus.rs:60`, `esp32/src/periph.rs:808` | zgadza się |
| `board` wybiera backend przez parametryzację `embedded_services` | `plugin.py:1178-1193` (`_services`) | **częściowo**: patrz 3.1 |

## 3. Nowe ustalenia

### 3.1 `embedded_services` nie przyjmie wartości `esp32sim` (średnie)

`_services()` w pytest-embedded (`plugin.py:1178-1193`) rzuca `UnknownServiceError` dla każdej
nazwy spoza `SERVICE_LIB_NAMES`, a dla pustej wartości zwraca `['base']`. Zdanie z sekcji 3
"`board` selects the backend by indirect parametrisation of `embedded_services`" działa więc tylko
połowicznie:

- dla backendu `serial` parametr to `esp` (lub `serial,esp`), nazwa z rejestru;
- dla backendu `esp32sim` parametr musi być **pusty**, co pytest-embedded zamienia na bazowy
  `Dut` (`Dut(pexpect_proc, msg_queue, app, ...)`, `dut.py`). Pusty parametr spada jednak na
  opcję CLI (`getattr(request, 'param', None) or request.config.getoption(...)`), więc sesja
  uruchomiona z `--embedded-services esp` zepsuje przypadek `esp32sim` (pytest-embedded spróbuje
  otworzyć port).

Dobra wiadomość: bazowy `Dut` wystarcza. pytest-embedded wystawia fixture `msg_queue`
(`plugin.py:797`) i pompuje tę kolejkę do `pexpect_proc` (`_listener`, `plugin.py:810`);
`DuplicateStdoutPopen(msg_queue, cmd, meta)` (`log.py`) pisze do niej stdout procesu. To jest
dokładnie kształt pytest-embedded-qemu: backend `esp32sim` spawnuje runner jako
`DuplicateStdoutPopen` z fixture'owym `msg_queue`, a bazowy `dut.expect` widzi konsolę. M0 już
tak mówi.

**Poprawka tekstu.** W sekcji 3: `esp32sim` = brak usług (bazowy `Dut`, runner podpięty przez
`msg_queue`); `serial` = `esp` przez parametryzację; `--embedded-services` nigdy nie jest podawane
na CLI, a `--esp32tl-backend` zawęża parametryzację. Fixture `app` dla `esp32sim` dostaje ścieżkę
firmware'u z parametryzacji `app_path`, nie z `--app-path`, z tego samego powodu.

### 3.2 "Unknown reply at once" łamie regułę 2 protokołu (średnie)

4.4 reguła 2: nieznane żądanie dostaje `unknown_reply` "at once". 4.1 reguła 2: `t` wcześniejsze
niż bieżący czas emulowany to błąd protokołu. Runner może opróżnić wiadomość `uart_tx` dopiero w
punkcie blokady, czyli gdy jest już w `t_req + latency`. Odpowiedź "natychmiast", rozumiana jako
`t = t_req`, jest wtedy spóźniona i kończy bieg błędem, dokładnie w przypadku, który specyfikacja
każe obsłużyć odpowiedzią, nie awarią.

Druga strona tego samego szwu: reguła 1 pozwala skwitować zdarzenie samym `wait`, a odpowiedzieć
później. Jeśli peer skwituje żądanie bez ustawienia `quiet_until ≤ t_req + d`, runner pobiegnie
dalej i odpowiedź przyjdzie spóźniona. Akapit w 4.4 wylicza `quiet_until` tylko z "the next
unprompted event".

**Poprawka tekstu.** W 4.1 zdefiniować "at once" jako `t_req + latency` (zawsze bezpieczne, bo
runner blokuje najpóźniej w `t_first_unacked + latency ≤ t_req + latency`). W 4.4: `quiet_until`
to minimum z następnego zdarzenia niewywołanego i wszystkich odpowiedzi oczekujących na wysłanie.
Oba przypadki dodać do testu fałszywego peera w M1 (po stronie Rust: odpowiedź dokładnie w
`t_req + latency` przechodzi; po stronie Pythona: test jednostkowy silnika dopasowań, że
nieznane żądanie i odłożona odpowiedź ustawiają `quiet_until`).

### 3.3 Brak kanału diagnostycznego runner → harness (średnie)

Tabela 4.1 nie ma wiadomości, którą runner mógłby zgłosić, co zgubił lub odrzucił: przepełnienie
RX FIFO (`RXFIFO_OVF`, ciche z punktu widzenia peera), `FRM_ERR` przy niezgodnym baudzie,
`uart_rx` na nieskonfigurowaną trasę ("Unrouted input is dropped", `board.rs:62`), `gpio_in` na
pin, który firmware ma jako wyjście. Dziś jedyne ujście to stderr runnera, którego test nie
czyta. Specyfikacja każe liczyć i raportować nieznane żądania po stronie peera; symetryczne
zdarzenia po stronie emulatora znikają.

**Poprawka tekstu.** Dodać `warn` *ext* (runner → harness: `t`, `kind`, `detail`) i `stats`
wysyłane przy `stop` (liczniki per trasa: bajty, przepełnienia, błędy ramki, odrzucone wejścia).
`BoardLink` zbiera je do raportu testu obok "unseen requests". Zasięg: M1 dla `warn` (bo `FRM_ERR`
jest dokładnie tym, co `uart_cfg` ma wyłapać), `stats` w M2 razem z raportem.

### 3.4 Drobne

- **Scenariusz a `quiet_until`.** Zdarzenia `gpio_in`/`adc` z `at=` idą harness → runner z
  przyszłym `t`. Runner musi je kolejkować po `t` (dla `adc` może użyć `machine.script`, które jest
  `pub`, albo własnej kolejki), a `BoardLink` musi wliczać oś czasu scenariusza do `quiet_until`,
  inaczej runner przebiegnie obok zaplanowanego zdarzenia. Jedno zdanie w 4.1 i w M3.
- **GPIO 15 w komentarzu 4.5.** Zdanie "GPIO 12 or 15, which do [change the boot mode]" jest
  nieścisłe: GPIO 12 ustawia napięcie VDD_SDIO (może zablokować boot), GPIO 15 wycisza log ROM na
  U0TXD i razem z GPIO 5 ustawia timing SDIO slave. Dla harnessu asercji na konsoli to GPIO 15
  jest ciekawsze: niski poziom przy boocie usuwa banner ROM, którego może oczekiwać test. Zapisać
  tak, jak jest.
- **`stop` bez odpowiedzi.** Nie jest zapisane, czym runner kończy po `stop` (ostatni `sync`,
  `stats`, kod wyjścia). Zamyka się razem z 3.3.

## 4. Co zrobić

1. Poprawić zdanie o `embedded_services` w sekcji 3 (3.1); to jedyna rzecz, która inaczej wyjdzie
   dopiero w M0 jako wyjątek pytest-embedded.
2. Dopisać definicję "at once" i `quiet_until` z odpowiedzi oczekujących (3.2) przed pisaniem
   `docs/protocol.md` w M1; dodać oba przypadki do testów.
3. `warn`/`stats` (3.3) i drobne (3.4) przy pisaniu `docs/protocol.md`.
4. Start M0.
