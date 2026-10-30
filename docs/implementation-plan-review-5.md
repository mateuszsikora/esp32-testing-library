# Piąte review planu implementacji esp32tl

Przegląd `docs/implementation-plan.md` (651 linii, zapis z 2026-10-29 21:39) po czterech
poprzednich review. Sprawdzony tego samego dnia względem issue #1 (bez zmian), `mikroverk/esp32sim`
na `2e36224` (#325 nadal otwarte, bez komentarzy) i `espressif/pytest-embedded` (main, PyPI 2.9.3).

## Werdykt

**Plan jest gotowy do startu M0. Nie ma ustaleń blokujących.**

Rewizja zamyka wszystkie ustalenia z czwartego review zmianami w treści; każde sprawdziłem
(sekcja 1). Nowe twierdzenia rewizji zgadzają się ze źródłami (sekcja 2).

Piąte review zeszło o poziom niżej niż poprzednie: do pętli wykonania esp32sim, żeby sprawdzić,
czy hooki, na których stoją reguły protokołu, są na klasycznym chipie w ogóle wołane i kiedy.
Wynik jest dobry (sekcja 3): `advance_to` jest wołane co tick, `uart_rx` odpytywane co kwant 64
instrukcji, a termin płytki ogranicza przeskok bezczynnego rdzenia. Z tego sprawdzenia wyszło
jedno ustalenie średnie (jedno zdanie w 4.2 plus jeden przypadek testowy w M1) i trzy drobne.
Po pięciu rundach lista nowych ustaleń kurczy się z pięciu blokujących do zera, a średnich z
dwunastu do jednego; dalsze review przed kodem nie zwrócą kosztu. Następne powinno czytać
`docs/protocol.md` i test fałszywego peera z M1, nie plan.

## 1. Zamknięcie ustaleń z czwartego review

| Ustalenie | Gdzie zamknięte | Sprawdzone |
|---|---|---|
| 3.1 `esp` nie wgrywa firmware'u | Architektura (`pytest-embedded-idf[serial]`), sekcja 3 "Fixtures", M4, 4.3 (`app_sha256` vs wgrane) | tak; patrz 4.2 niżej |
| 3.2 PPP jako `real` w przebraniu | M6, 4.3 (zdarzenie zmiany zegara), sekcja 6, tabela ryzyk | tak |
| 3.3 Fixture nie parametryzuje fixture'a | Sekcja 3: `pytest_generate_tests`, jedno `metafunc.parametrize` z parą nazw | tak |
| 3.4 Warianty `Stop`, `Halted` w M9 | 4.2, M9 | tak |
| 3.5 `machine.script` jako kolejka | Akapit pod tabelą 4.1: własna kolejka, `machine.script` odrzucone z uzasadnieniem | tak |

Deklaracja z sekcji 10 planu zgadza się ze stanem tekstu.

## 2. Weryfikacja nowych twierdzeń rewizji

| Twierdzenie | Źródło | Wynik |
|---|---|---|
| `pytest-embedded-idf[serial]` ciągnie `pytest-embedded-serial-esp`, a to `pytest-embedded-serial` | PyPI `requires_dist` obu pakietów | zgadza się |
| `idf` daje dekoder paniki | `pytest-embedded-idf` zależy od `esp-idf-panic-decoder` | zgadza się |
| `IdfApp.flash_files` z `flasher_args.json` | `pytest_embedded_idf/app.py:25,47,87` | zgadza się |
| `machine.rt.enabled` to wszystko, co ustawia `--realtime` | `cli/src/lib.rs:829` | zgadza się |
| `Stop::Halted` = `max_cycles`; lista wariantów | `esp-soc/src/machine.rs:584`, `esp-soc/src/soc.rs:9-27` | zgadza się |
| `Script.events` z kursorem `pos` | `esp-soc/src/machine.rs:40` | zgadza się |

## 3. Pętla wykonania esp32sim a kontrakty planu

Sprawdzone na klasycznym chipie, bo na nim stoją reguły 1-4 protokołu, `uart_cfg`, stemplowanie
w M5 i pacing z ustalenia 8:

| Założenie planu | Co robi esp32sim | Wynik |
|---|---|---|
| `advance_to` jest wołane regularnie (reguła 3, odpytywanie `uart_cfg`, stempel M5) | `tick` → `deliver_board_inputs` → `board.advance_inputs_with_clock(cycles)` → domyślnie `advance_to(cycles)` (`esp32/src/bus.rs:350,636`, `esp-soc/src/board.rs:137`); `tick` idzie co kwant harmonogramu (64 instrukcje natywnie, `machine.rs:78`) | trzyma się |
| `uart_rx` odpytywane dość gęsto, by pacing per znak miał sens | `board.uart_rx(self.cycles)` w tym samym `tick` (`bus.rs:637`); kwant 64 instrukcji to ułamek mikrosekundy przy 240 MHz, czas znaku przy 115200 bd to 87 µs | trzyma się |
| termin płytki zatrzymuje emulator dokładnie w punkcie blokady, także gdy rdzeń śpi w WFI | `SocBus::next_deadline` klasycznego chipa łączy terminy timerów z `board.next_deadline()` (`esp32/src/soc.rs:106-112`); `idle_budget`, `vq_quanta` i `bb_quanta` ograniczają nim przeskok i partie kwantów (`machine.rs:776,806,907,959`) | trzyma się, pod warunkiem z 4.1 |
| pod `--realtime` emulator nie łączy kwantów w partie | `vq_quanta` zwraca 1 przy `rt.enabled` (`machine.rs:777`) | trzyma się |
| `next_deadline` bez terminu nie psuje niczego | `None` → jeden kwant na raz (`machine.rs:776,806`) | trzyma się, kosztem szybkości |

## 4. Nowe ustalenia

### 4.1 `next_deadline` płytki musi zawierać `t_first_unacked + latency` (średnie)

4.2: "The `next_deadline` is the earlier of the next paced byte and the peer's `quiet_until`".
Reguła 1 każe blokować w `min(quiet_until, t_first_unacked + latency)`. Jeśli trzeci składnik
nie wejdzie do `next_deadline`, bezczynny rdzeń (firmware czeka w WFI na odpowiedź modemu, czyli
najczęstszy przypadek) dostanie od `idle_budget` przeskok do następnego timera, który może leżeć
milisekundy za `t_req + latency`. Runner obudzi się w `uart_rx(cycle)` już po terminie, peer
odpowie w `t_req + latency` zgodnie z regułą 2, a runner uzna ją za spóźnioną. Błąd byłby
samowzbudny i losowy względem tego, czy firmware akurat spało.

**Poprawka.** Jedno zdanie w 4.2: `next_deadline` = minimum z trzech: następny bajt z pacingu,
`quiet_until`, `t_first_unacked + latency`. Do testu fałszywego peera w M1 dodać przypadek:
gość w WFI, żądanie wysłane, odpowiedź dokładnie w `t_req + latency` przechodzi. Obecny przypadek
"an answer stamped exactly `t_req + latency` passing" nie wymusza bezczynnego rdzenia, więc może
przejść bez tej poprawki.

### 4.2 `IdfSerial` wgrywa sam przy starcie; harness nie powinien wgrywać drugi raz (drobne)

M4: "Before each test the harness flashes the CI artifact with `IdfSerial.flash`".
`IdfSerial._start` już to robi, chyba że podano `--skip-autoflash`
(`pytest_embedded_idf/serial.py:68-80`); pytest-embedded ma też `--erase-all` i `--erase-nvs`.
Wywołanie `flash()` z esp32tl dołożyłoby drugie wgranie na test (kilkanaście sekund każde).

**Poprawka.** M4: harness polega na autoflashu usługi `idf`; `--skip-autoflash` zostaje dla
iteracji na już wgranej płytce, a wtedy sprawdzenie `app_sha256` wobec artefaktu jest jedyną
gwarancją, że test biegnie na tym obrazie, który deklaruje. Jedno zdanie.

### 4.3 Resztki starego brzmienia po zmianie usług (drobne)

Ustalenie 1 nadal mówi "delegates the `serial` backend to pytest-embedded's `serial` and `esp`
services", a drzewo w sekcji 3 "wraps pytest-embedded's serial/esp Dut". Po czwartym review
backend `serial` to usługi `esp,idf` i `IdfDut`. Dwie frazy.

### 4.4 Przeliczenie ns ↔ cykle nieopisane (drobne)

Protokół mówi w nanosekundach, trait w `VirtualCycle`. `bus.cycles()` biegnie w stałym
odniesieniu niezależnym od chwilowego taktu CPU (`tick` skaluje przez `cpu_scale_half`,
`bus.rs:631-634`; `Machine::seconds` dzieli przez `S::CPU_HZ`, `machine.rs:166`), więc
`ns = cycles · 10⁹ / CPU_HZ` jest stałe przez cały bieg, także po przełączeniu XTAL → PLL przy
boocie. To dobra wiadomość, której plan nie zapisuje; `docs/protocol.md` powinno podać ten wzór
i źródło `CPU_HZ`, żeby peer w innym języku nie musiał tego odkrywać.

## 5. Co zrobić

1. 4.1 wprowadzić do 4.2 i do listy testów M1 teraz; to jedyna rzecz z tego review, która może
   dać fałszywy błąd w M1.
2. 4.2 do 4.4 przy pisaniu `docs/protocol.md` i M4.
3. Start M0. Kolejne review robić na `docs/protocol.md` i na teście fałszywego peera, nie na
   planie.
