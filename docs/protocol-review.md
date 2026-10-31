# Review `docs/protocol.md` (#10)

Przegląd nowego `docs/protocol.md` (559 linii, nieśledzony) i dwóch notatek dopisanych do
`docs/implementation-plan.md` (4.1 i 4.2). Sprawdzony 2026-10-31 względem źródeł esp32sim na pinie
projektu `8c65215` (checkout cargo), kodu `runner/src/main.rs`, issue #10, #11, #12, #23 i
`mikroverk/esp32sim#325` (nadal otwarte, bez komentarzy).

## Werdykt

**Kontrakt jest gotowy do zamknięcia #10 po dwóch poprawkach treści i aktualizacji #11.**
Nie ma ustaleń blokujących. Wszystkie odniesienia do kodu esp32sim zgadzają się co do linii (sekcja
1), arytmetyka czasu jest poprawna, a każdy punkt checklisty #10 jest pokryty (sekcja 3). Projekt
tur (`sync` tylko w punkcie blokady, odczyt gniazda tylko w turze peera) jest spójny i rzeczywiście
odcina wynik od szybkości peera w czasie ściennym.

| Waga | Liczba |
|---|---|
| Blokujące | 0 |
| Średnie | 3 |
| Drobne | 7 |

Średnie:

1. Opis przekroczenia punktu blokady w 6.1 jest nieprawdziwy co do mechanizmu (2.1).
2. Brak sposobu na zakończenie biegu ze `stats`, gdy harness kończy test między turami (2.2).
3. Checklista #11 i fragmenty planu opisują projekt, który ten dokument zastępuje (2.3).

## 1. Weryfikacja twierdzeń względem esp32sim `8c65215`

| Twierdzenie w `protocol.md` | Źródło | Wynik |
|---|---|---|
| `CPU_HZ` = 240 000 000 | `esp32/src/periph.rs:18` | zgodne |
| `tick` skaluje przez `cpu_scale_half` | `esp32/src/bus.rs:631` | zgodne |
| `Machine::seconds` dzieli przez `CPU_HZ` | `esp-soc/src/machine.rs:166` | zgodne |
| `ns = floor(c·25/6)`, `cycles = ceil(ns·6/25)`; cykl → ns → cykl daje ten sam cykl | arytmetyka | zgodne (6/25 < 1, więc `ceil` wraca do `c`) |
| `cycles × 10⁹` przepełnia u64 po 76 s; 2⁵³ ns = 104 dni | arytmetyka | zgodne (76,9 s; 104,2 dnia) |
| `uart_tx` przy zapisie FIFO, z trasą | `esp32/src/bus.rs:308-311`; alias AHB `0x6000_0000` mapowany wcześniej (`bus.rs:231-233,270`) | zgodne; zapis przez alias AHB, którego używa sterownik IDF, też trafia w hook |
| `uart_route(port)` | `esp32/src/periph.rs:817` | zgodne (plan miał 808, dokument poprawił) |
| `baud` = `null` przy zerowym dzielniku | `esp-periph/src/uart.rs:80-85` (`classic_baud`) | zgodne |
| FIFO RX 128 B, nadmiar odrzucony z `RXFIFO_OVF` | `esp-periph/src/uart.rs:9,43-46` | zgodne |
| `matches_baud`: 3 % baud wiadomości, `null` = niezgodność, `FRM_ERR` i odrzucenie | `esp-soc/src/uart.rs:21-23,73-79` | zgodne |
| "Unrouted input is dropped" | `esp-soc/src/board.rs:61` | zgodne |
| `rx_pending()` dostępne z pętli | `esp-periph/src/uart.rs:73`, `ClassicUart(pub Uart)` (`periph.rs:537`), `pub periph` (`bus.rs:60`) | zgodne |
| `reboot()` zwraca przyczynę, nie rusza licznika cykli | `esp-soc/src/machine.rs:306`, `esp32/src/soc.rs:132-157` | zgodne |
| `reboot()` wymienia peryferia | `esp32/src/soc.rs:138` | zgodne |
| Przyczyny 3, 5, 9, 12, 13, 16 | `esp-periph/src/rtc_cntl.rs:7-8,38-40,68`, `esp32/src/sleep.rs:66` | zgodne; watchdogi TG (7, 8, 11, 17) nie są modelowane, więc lista jest pełna |
| Płytka przeżywa reset, poziomy wejść zostają | `soc.rs:140-155`: `restore_inputs`, `restore_rtc_analog(old.adc)`, `attach_board_devices` | zgodne; źródła analogowe z `analog_set` też przeżywają (cały `adc` przenoszony) |
| `SocBus::analog_set(pin, AnalogSource::Const(volts))` | `esp32/src/soc.rs:213`, `esp-periph/src/analog.rs:12-13` | zgodne (`Const(f32)`) |
| Lista wariantów `Stop` | `esp-soc/src/soc.rs:9-27` | zgodne |
| `Stop::Halted` przy `max_cycles` | `esp-soc/src/machine.rs:584,607,758` | zgodne; to jedyne źródło `Halted` w `run` (poza `ScriptAction::Stop`, którego runner nie używa) |
| Pomijanie bezczynności ograniczone `max_cycles` i `IDLE_CHUNK` = 512 | `machine.rs:600`, `esp32/src/soc.rs:24` | zgodne |
| Szyna klasyczna nie odracza ticków | `esp-soc/src/soc.rs:148`, brak nadpisania w `esp32/src` | zgodne |
| Kwant 64 instrukcje natywnie | `machine.rs:132` | zgodne |
| **Zajęte rdzenie kończą rundę, przekroczenie do jednego kwantu** | `machine.rs:686-691` | **niezgodne**, sekcja 2.1 |

Kody wyjścia 0, 1, 2 zgadzają się z tym, co już robi `runner/src/main.rs:7-11`.

## 2. Ustalenia

### 2.1 (średnie) Runda nie kończy się za `max_cycles`: przekroczenie jest poniżej jednego cyklu CPU

6.1 mówi: "`run` stops at the first scheduling boundary at or after `max_cycles`: busy cores finish
the round they are in (one quantum each, 64 instructions natively)". Kod robi co innego. Na ścieżce
bez modelu kosztów kwant rundy jest przycinany do pozostałych cykli (`machine.rs:691`:
`quantum.min(self.bus.cpu_cycles_for_bus(self.max_cycles - self.bus.cycles()))`), a komentarz
`run` mówi to wprost: "the cycle ceiling cuts the final round short" (`machine.rs:503-504`). Wirtualne
kwanty i łączenie rund, które mogłyby to obejść, wymagają `can_defer`, a klasyczna szyna go nie ma.

Wynik: runner dostaje sterowanie w `T`, pierwszej granicy cyklu CPU w `B` lub za nim.
`cpu_cycles_for_bus` zaokrągla w górę (`esp32/src/soc.rs:103-105`), więc `T − B` jest mniejsze niż
jeden cykl CPU w jednostkach szyny: 0 przy 240 MHz, poniżej 6 cykli szyny (25 ns) przy 40 MHz XTAL
w czasie startu (`cpu_scale_half`, `periph.rs:989-992`).

Co z tego wynika:

- Trzy konsekwencje z 6.1 pozostają prawdziwe jako górne ograniczenia, ale "less than one quantum"
  i "at most one quantum later" przeszacowują opóźnienie około 64 razy. Wejście wchodzi w życie
  najwyżej jeden cykl CPU po swoim `t`.
- Uzasadnienie w 11.3 ("whenever the guest was mid-quantum at B") jest błędne. Sama reguła
  (mierzyć względem `sync t = B`, nie względem bieżącego czasu) jest nadal potrzebna, bo
  zaokrąglenie przy XTAL i konwersja ns ↔ cykle nadal mogą dać `T > B`.
- Gałąź reguły 1 "nowe `B` już za gościem" w praktyce zdarzy się prawie tylko przy XTAL. Testy
  fake-peer w #11 nie powinny zakładać, że łatwo ją wywołać przy 240 MHz.

Propozycja: przepisać akapit 6.1 na rzeczywisty mechanizm (przycięcie ostatniej rundy, `T − B` poniżej
jednego cyklu CPU) i poprawić uzasadnienie w 11.3. Warto dopisać jedno zdanie, że
`Machine::run_until_cycle` (`machine.rs:917-929`, dokładne zatrzymanie i `Yield` na zdarzenie
hosta) odpada, bo obsługuje tylko jeden rdzeń, a klasyczny ESP32 ma dwa. Bez tego zdania
implementujący #11 trafi na tę funkcję i straci czas.

### 2.2 (średnie) Harness nie może zakończyć biegu ze `stats` między turami

`stop` jest dozwolone tylko w turze peera, a tura przychodzi dopiero w `B`. Przy `quiet_until` i
`latency` równych `null` (dozwolone; strumień GNSS bez zdań z odstępem też tak może wyglądać) tura
nie przyjdzie do `--max-seconds`. Sekcja 9 mówi, że harness, który chce skończyć wcześniej, zabija
proces i nie dostaje `stats`. To jest typowe zakończenie testu: `console.expect` się dopasowuje,
teardown zamyka DUT. Skutki:

- `stats` ginie w każdym takim teście, a M2 zakłada "`stats` at `stop`, merged with the engine's
  unseen-request list into one report per test".
- Scalony `warn` czeka, aż "any other message is due" (5.8). Bez `sync` może czekać do końca biegu,
  więc ostatnie ostrzeżenia też giną, a to te, które tłumaczą nieudany test.

Propozycja, jedna z dwóch, zapisana w dokumencie:

- Runner obsługuje SIGTERM (i SIGINT): kończy bieżący wycinek (najwyżej 1 ms emulowanej), wysyła
  wstrzymane zdarzenia i `warn`, `stats` z `end: signal`, zamyka gniazdo, wychodzi z 0 albo z
  osobnym kodem. Nie psuje determinizmu: dotyczy tylko końca biegu.
- `BoardLink` zawsze ogranicza `quiet_until` do stałego rytmu (np. 10 ms emulowanej), żeby `stop`
  było możliwe z ograniczonym opóźnieniem. Według pomiaru z #325 wymiana kosztuje około 19 µs, więc
  100 tur na emulowaną sekundę to około 2 ms czasu ściennego.

Niezależnie od wyboru: scalony `warn` powinien wychodzić najpóźniej na granicy najbliższego wycinka
milisekundowego, nie dopiero przy następnej wiadomości.

### 2.3 (średnie) #11 i fragmenty planu opisują zastąpiony projekt

#11 jest następny w kolejce i plan każe zrobić go pierwszy (§9). Jego checklista nadal zawiera:

- `SocketBoard` z `uart_rx`, `next_deadline`, `advance_to`; protokół dostarcza bajty z pętli przez
  `bus.periph.uart_pin_input` (6.1), więc `BoardModel::uart_rx` nie jest używany, a `next_deadline`
  znika (11.10);
- "`next_deadline` = min(next paced byte, `quiet_until`, `t_first_unacked + latency`)";
- "`sync` at every `advance_to`, at most once per emulated ms" (zastąpione przez 11.2);
- `warn` dla `Unimplemented`/`Exceptions` (11.8 przenosi to do `stats.end`);
- test "same with the guest idle in WFI" bez informacji, że teraz testuje wycinki.

11.10 mówi "#11's checklist items ... follow this section". Nie wiadomo, czy to polecenie
aktualizacji, czy stwierdzenie; stan issue wskazuje, że nikt go nie wykonał.

W planie notatka w 4.2 wymienia tylko `next_deadline`, a 4.2 zawiera jeszcze `uart_rx` i `advance_to`
na liście metod oraz "`Unimplemented` and `Exceptions` also go out as `warn`". Punkt M1 (linie
436-442) nadal tłumaczy test WFI "trzecim składnikiem `next_deadline`".

Propozycja: zaktualizować treść #11 w tym samym PR, który wprowadza `protocol.md`. Notatkę w 4.2
zmienić na ogólną ("where 4.2 and `docs/protocol.md` differ, the protocol holds: run-loop slices
instead of `next_deadline`, delivery through `uart_pin_input`, emulator stops in `stats.end`").
Dodać podobny odsyłacz przy M1. W 11.10 napisać wprost, co zmienia się w #11.

### 2.4 (drobne) Niezgodność wersji wykryta przez harness kończy się kodem 4, nie 3

Sekcja 4 mówi "A mismatch on either side ends the run (runner: exit code 3)". Harness nie może wysłać
`stop` przed handshake'iem, więc jedyne, co może zrobić, to zamknąć gniazdo, a wtedy runner wychodzi
z kodem 4 ("the peer is gone"). Propozycja: harness wysyła swój `hello` także przy niezgodności, a
runner wykrywa ją sam i wychodzi z 3. Alternatywnie zapisać, że niezgodność wykryta przez harness
daje 4.

### 2.5 (drobne) Brak zachowania, gdy peer przestaje odpowiadać

Runner w turze peera czyta gniazdo blokująco i bez limitu czasu. #325 obiecuje upstreamowi "never
waits silently". Zawieszony peer w osobnym procesie (model w innym języku, co plan wprost
przewiduje) zawiesza job CI. Propozycja: `--peer-timeout SECONDS` w czasie ściennym, kod 4 i linia na
stderr. Działa tylko na ścieżce błędu, więc nie dotyka determinizmu.

### 2.6 (drobne) `t` i `port` ostrzeżeń wykrywanych przez runner

- Bajt zaplanowany na `d_k` jest dostarczany na granicy wycinka `T ≥ d_k`. Dokument nie mówi, które
  z nich trafia do `t` w `warn` dla `rxfifo_ovf`/`frm_err`/`unrouted_rx`. Propozycja: `T`
  przeliczone na ns, ten sam czas, który dostaje `uart_cfg` ("where the runner saw").
- `uart_pin_input` dostarcza bajt do każdego UART-u, którego `rx_pin` pasuje
  (`esp-soc/src/uart.rs:73-79`). Przy dwóch portach na jednym pinie są dwa spadki na bajt.
  Propozycja: jeden `warn` na port; dopisać zdanie.

### 2.7 (drobne) Luki w semantyce `watch`

- Wejścia (`uart_rx`, `gpio_in`, `release`, `adc`) na pinach spoza `watch`: dozwolone, błąd
  protokołu czy ignorowane? Nie jest to napisane.
- `uart_tx` jest przekazywany dla trasy, której tylko pin RX jest obserwowany (5.1). Takie bajty nie
  trafiają na żaden obserwowany przewód, a jako zdarzenia z `seq` wymuszają tury przy skończonym
  `latency`. Propozycja: `uart_tx` tylko gdy `tx_pins ∩ watch ≠ ∅`; `uart_cfg` dalej dla obu
  pinów, bo z niego peer odbiorczy poznaje baud.

### 2.8 (drobne) Granice wiadomości `uart_tx` zależą od flag i od peera

Wiadomość kończy się przy "any other runner message is due" (to obejmuje `console` przy `--console
socket|both`) i w punkcie blokady, a ten zależy od `latency`/`quiet_until` peera. Ten sam gość daje
więc inne podziały przy innym trybie konsoli lub innym źródle, a tap zapisuje jedną linię na
wiadomość. Test determinizmu z §6 planu porównuje dwa biegi z tymi samymi flagami, więc dziś to nie
szkodzi. Propozycja: jedno zdanie w 5.2, że granice wiadomości nie niosą znaczenia poza `t`
pierwszego bajtu, a konsument (dopasowywanie, tap) skleja bajty na trasie.

### 2.9 (drobne) Sekcja 10 pomija rozbieżność z #325 widoczną na drucie

#325 proponuje tempo odbioru "at the route's baud"; dokument używa baud wiadomości (11.6). Tabela
"What #325 does not have" tego nie wymienia, a to zmienia moment `frm_err` i tempo bajtów przy
niezgodnym baud. Propozycja: dopisać wiersz.

### 2.10 (drobne) Drobiazgi redakcyjne

- 5.5: "(outputs disabled)" jako stan po resecie lepiej zastąpić "the state read from the bus after
  `reboot()`"; IO_MUX może prowadzić pin (np. U0TXD na GPIO1) bez bitu w `GPIO_ENABLE`.
- Sekcja 9 nie mówi, co się dzieje z zakolejkowanymi wejściami i bajtami w trakcie pacingu na końcu
  biegu. Licznik w `stats` (np. `inputs_not_applied`) pozwoliłby `BoardLink` wykryć scenariusz,
  który wyszedł poza `--max-seconds`.
- Linia 385 ma 101 znaków, reszta prozy mieści się w 100.

## 3. Pokrycie checklisty #10

| Punkt #10 | Gdzie | Stan |
|---|---|---|
| NDJSON, Unix socket, TCP, ns | 1, 2, 3 | pokryte |
| Tabela wiadomości z *ext*, `hello` z `version`, `chip`, `clock`, `board` | 5, 5.1 | pokryte (`clock` tylko od runnera, uzasadnione w 11.7) |
| Reguły 1-5 | 6 | pokryte; rytm `sync` zmieniony i uzasadniony (11.2) |
| `latency` związane ze źródłem | 7 | pokryte |
| Przyszłe `gpio_in`/`release`/`adc` w kolejce runnera | 8 | pokryte, rozszerzone o `uart_rx` |
| `ns = cycles × 10⁹ / CPU_HZ` i skąd `CPU_HZ` | 3 | pokryte |
| `stop` i kody wyjścia | 5.14, 9 | pokryte; patrz 2.2, 2.4 |

## 4. Co sprawdziłem i uznałem za poprawne

- Model tur: odczyt gniazda tylko między `sync` a `wait` i zakaz pisania poza turą usuwają
  zależność od czasu ściennego i zakleszczenie przy pełnych buforach. Uzasadnienie zmiany względem
  planu (11.1) jest trafne: w starym projekcie wcześnie przeczytany `wait` przesuwał `B`.
- Kolejność: zdarzenia z `(B, T]` wychodzą po turze, więc peer w każdej turze widzi dokładnie
  zdarzenia do `B`.
- Powtórzenie `sync t` przy `latency` 0 jest możliwe i dokument to opisuje: wszystkie instrukcje
  rundy widzą ten sam `bus.cycles()` (czas rośnie w `after_round`), więc zdarzenie po turze może
  dostać `t` równe `B`.
- Wycinki zamiast `next_deadline` (11.10): pominięcie bezczynności ograniczone do 512 cykli i do
  `max_cycles`, brak odraczania na klasycznej szynie. Wniosek, że `SocketBoard` nie potrzebuje
  `next_deadline`, jest poprawny dla tego pinu.
- Wybór `run` + `max_cycles` zamiast `run_until_cycle` jest właściwy (dwa rdzenie), choć dokument go
  nie uzasadnia (2.1).
- Wykrywanie spadków w pętli runnera jest konieczne: `uart_pin_input` nie zgłasza spadku na
  nietrasowanym pinie, a `FRM_ERR` i `RXFIFO_OVF` są tylko bitami w `int_raw`.

## 5. Kolejność poprawek

1. 6.1 i 11.3: mechanizm przekroczenia (2.1).
2. Sekcja 9 i 5.8: zakończenie biegu ze `stats` i limit scalania `warn` (2.2).
3. #11: treść issue; plan 4.2 i M1: odsyłacze (2.3).
4. Drobne 2.4-2.10, w dowolnej kolejności; żadne nie zmienia kodu, który ktoś już napisał.
