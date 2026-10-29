# Review planu implementacji esp32tl

Przegląd `docs/implementation-plan.md` z 2026-10-29, sprawdzony tego samego dnia względem
issue #1 (specyfikacja), `mikroverk/esp32sim` na `2e36224`, `espressif/pytest-embedded` (main,
PyPI 2.9.3) oraz środowiska na tej maszynie.

## Werdykt

Plan jest solidny i w większości zweryfikowalny: z 30 sprawdzonych faktów o esp32sim,
pytest-embedded i środowisku wszystkie się zgadzają (tabela w sekcji 4). Dziewięć ustaleń z sekcji 1
planu trzyma się źródeł co do linii. Architektura, kontrakty i kolejność kamieni milowych są
sensowne.

Przed startem M0/M1 trzeba jednak poprawić pięć rzeczy, bo dotyczą one ścieżki krytycznej albo
obietnic, których plan w obecnej postaci nie spełni:

1. M1 opiera się na trasie GNSS w profilu płytki, której profil nie ma i mieć nie może bez schematu.
2. Źródło `real` (nagrywanie prawdziwego urządzenia, czyli połowa tezy specyfikacji) nie ma
   żadnego kamienia milowego.
3. `adc` nie jest hookiem `BoardModel`, więc runner nie jest czystym transportem trait'u i nie
   zniknie "w dniu, gdy #325 wejdzie".
4. Protokół 4.1 nie mówi, kiedy runner czeka na peera i co robi z odpowiedzią spóźnioną w czasie
   emulowanym. Od tego zależy deterministyczność z sekcji 6 i brak zakleszczeń w Pythonie.
5. Macierz CI zakłada Dockera na `macos-latest`, którego tam nie ma. Build firmware i PPP nie
   pójdą na macOS w GitHub Actions.

Żadna z tych rzeczy nie podważa kierunku planu. Każda ma poprawkę na akapit.

## 1. Ustalenia ważne (do poprawy przed M0/M1)

### 1.1 M1 potrzebuje trasy GNSS, której profil nie ma

Sekcja 4.5 planu pokazuje profil `lilygo-t-a7670g-r2` tylko z trasą `[uart.modem]` (GPIO 26/27)
i liniami modemu. Specyfikacja mówi wprost: pozostałe piny "come from the vendor schematic, not
from memory". Tymczasem M1 pisze: "UART1 on pins from `sdkconfig` (defaults to the profile's GNSS
route)". Takiej trasy nie ma, a na T-A7670G modem i tak zajmuje UART1.

**Poprawka.** M1 nie powinien zależeć od profilu LilyGO. `nmea_reader` jest firmware'em tego repo,
więc jego piny są nasze: dodać generyczny profil `esp32-devkitc.toml` z trasą `[uart.gnss]` na
dowolnych wolnych pinach (np. UART1 na 16/17 jak w IDF) i na nim przejść M0 do M3. Profil LilyGO
wchodzi, gdy ktoś zajrzy do schematu, i wtedy dostaje też trasę GNSS (albo adnotację, że GNSS
idzie przez AT modemu, co na tej płytce jest prawdopodobne).

### 1.2 Źródło `real` nie ma kamienia milowego

Specyfikacja: "a real device can be recorded so that the recording becomes the replay later" i
"Tap: ... wraps any source, `real` included". Architektura planu wymienia `sources/real.py` ("host
serial port, with wall-time stamping"), a M2 obiecuje "`tap(path)` on every source, including
`real`". Ale:

- na backendzie `esp32sim` prawdziwe urządzenie na porcie hosta wymaga mapowania czasu ściennego
  na emulowany i `--realtime`; issue #325 odkłada to jawnie na później ("the mechanics of
  stamping wall time as emulated time need their own design"), a plan nigdzie tego nie projektuje;
- na backendzie `serial` peryferia pokładowe są przylutowane, więc tap na "real" oznacza sniffer
  sprzętowy, którego plan nie przewiduje.

Żaden kamień milowy nie dostarcza `real`. Zdanie w M2 jest więc nie do spełnienia.

**Poprawka.** Dodać osobny kamień (między M3 a M4 albo jako M3b): `real` na `esp32sim` przez
`--realtime` runnera, ze zdefiniowanym stemplowaniem (czas ścienny odbioru bajtu przeliczony na
cykle emulowane w chwili `advance_to`). W tabeli zdolności zapisać, że na `serial` tap jest
możliwy tylko na konsoli. M2 zawęzić do "tap na `model` i `replay`".

### 1.3 ADC nie jest hookiem `BoardModel`

Trait `BoardModel` (`esp-soc/src/board.rs:26-130`) nie ma żadnej metody ADC. Na klasycznym ESP32
napięcia wchodzą przez `--script` (`adc`, `adcwave`; `esp-soc/src/machine.rs:1156-1162`), czyli
przez maszynę, nie przez płytkę. Issue #325 też nie wspomina o ADC.

Skutki dla planu:

- wiadomość `adc` z tabeli 4.1 nie może być zrealizowana przez `SocketBoard`; runner musi ją
  obsłużyć osobno, sięgając do `machine.bus`;
- zdanie z 4.2 "Dropped the day #325 ships" jest prawdziwe dla UART/GPIO, fałszywe dla ADC;
- ryzyko z sekcji 7 "#325 declined or stalled" nie ujmuje tego, że nawet przyjęte #325 zostawia
  ADC po naszej stronie.

**Poprawka.** W 4.1 oznaczyć `adc` jako rozszerzenie runnera poza #325. W M3 zapisać, że ADC idzie
przez API maszyny. W M8 dodać punkt: zaproponować upstream hook ADC w `BoardModel` albo ścieżkę
`--script`-podobną sterowaną z gniazda, inaczej runner zostaje na stałe (co jest w porządku, ale
trzeba to powiedzieć).

### 1.4 Protokół 4.1 nie definiuje czekania ani spóźnień

Reguła "the runner never blocks on the peer except at `min(quiet_until, first unanswered event +
latency)`" zakłada, że peer odpowiada na każde zdarzenie. Nie jest zapisane:

- czy każde `uart_tx` / `gpio_out` wymaga odpowiedzi (dane albo odnowione `wait`), czy peer może
  milczeć; bez tego każdy bajt z firmware'u kosztuje blokadę do `t + latency`, a peer, który
  "nie ma nic do powiedzenia", musi i tak coś wysłać, żeby runner ruszył;
- co robi runner, gdy `uart_rx.t` jest wcześniejsze niż bieżący czas emulowany (peer odpowiedział
  po terminie); błąd, przycięcie do `now` z ostrzeżeniem, czy ciche przycięcie. Determinizm z
  sekcji 6 ("same recording twice gives identical console output") trzyma się tylko wtedy, gdy
  `t` pochodzi z nagrania, a nie z chwili dotarcia, i gdy spóźnienie jest błędem albo
  deterministycznym przycięciem;
- kadencja `sync` (co ile emulowanych ns, czy tylko przy `advance_to`);
- kolejność startu: czy runner czeka na pierwsze `wait` przed bootem gościa (inaczej peer, który
  wstaje wolniej niż firmware, traci pierwsze bajty konfiguracyjne GNSS).

**Poprawka.** Dopisać do 4.1 cztery zdania: (1) każde zdarzenie runner → peer ma numer i peer
kwituje je `wait` albo danymi; (2) `t` wcześniejsze niż bieżący czas to błąd protokołu kończący
bieg (peer złamał `latency`); (3) `sync` idzie przy każdym `advance_to` i nie częściej niż raz na
N ms emulowanych; (4) gość bootuje dopiero po pierwszym `wait`. Test jednostkowy w Rust z fałszywym
peerem (już w M1) powinien pokrywać (2) i (4).

### 1.5 Docker na `macos-latest` nie istnieje

M0: "GitHub Actions (ubuntu + macos)", `firmware/build.sh` buduje w obrazie `espressif/idf:v5.3.6`,
M4 uruchamia `pppd` w kontenerze. Hostowane runnery macOS GitHub Actions nie mają działającego
Dockera. Tabela ryzyk wspomina tylko "pppd in a container needs Docker in CI and locally".

**Poprawka.** Firmware buduje jeden job na ubuntu i publikuje artefakt (cache po tagu IDF i
ścieżce przykładu, jak plan już zakłada); job macOS pobiera artefakt i uruchamia tylko runner +
Python. Testy PPP mają marker `requires_docker` i skip z powodem, gdy Dockera brak. Lokalnie na
macOS Docker 24 jest, więc deweloper nic nie traci.

## 2. Ustalenia średnie

| # | Gdzie | Co | Poprawka |
|---|---|---|---|
| 2.1 | nagłówek, F5 | "esp32sim `main` at `2e36224` (v0.3.0)": tag v0.3.0 to `964c26c`, `main` jest 227 commitów dalej | Pisać "main at 2e36224 (227 commits after v0.3.0)" |
| 2.2 | 4.1, M1 | Gdy firmware nigdy nie nadaje na trasie (nmea_reader tylko czyta), peer nie poznaje skonfigurowanego baudu i ufa profilowi; niezgodność daje cichy `FRM_ERR` (`board.rs:62`) | Dodać wiadomość runner → harness `uart_cfg` (port, piny, baud) przy zmianie konfiguracji trasy; w M1 zapisać, że `sdkconfig` nmea_reader i nagranie 9600 bd muszą się zgadzać |
| 2.3 | 3, M0 | Dystrybucja runnera: `pip install esp32tl` nie przyniesie binarki Rust; "clean checkout, one command" działa tylko z cargo i siecią do git-dep | Zapisać wprost: `make setup` buduje runner z cache; w M8 prebuilt binarki per OS w GitHub Releases |
| 2.4 | 4.1 vs 4.3 | Dwa słowniki zegara: `hello.clock` ∈ {`emulated`, `wall`}, nagłówek nagrania ∈ {`emulated-fifo-write`, `wall`} | Jeden słownik, ten z nagrania |
| 2.5 | 4.1 | `hello.board` z runnera: runner nie zna nazwy płytki (jego płytka to gniazdo) | Pole wypełnia harness; runner wysyła `chip` |
| 2.6 | M3 | Zakres M3 łączy trzy tematy: GPIO/ADC/scenario, backend `serial`, skrypt porównawczy | Wydzielić backend `serial` do własnego kamienia; to osobna obietnica specyfikacji ("every test a real board can run, runs on one too") |
| 2.7 | 3, M0 | Nazwy fixture'ów vs pytest-embedded: wtyczka esp32tl definiuje `board`, `console`; pytest-embedded już ma `dut`, `app`, `serial`, `embedded_services`. Plan nie mówi, czy `console` to `dut.expect` ani jak `board` parametryzuje `embedded_services` | Zapisać: `console` opakowuje `dut` pytest-embedded; backend wybierany przez indirect parametrize `embedded_services` (plugin to wspiera: `_request_param_or_config_option_or_default`) |
| 2.8 | M5 | "StationLink API is to be checked for public access": sprawdzone, `pub fn attach_wifi` w `esp32/src/bus.rs` i `pub struct StationLink` w `esp-soc/src/wifi.rs:490` | Usunąć zastrzeżenie |
| 2.9 | M5 | "If the classic SPI hooks cannot carry it": klasyczna magistrala woła `board.spi_transfer_pins` gdy `uses_spi_pins()` (`esp32/src/spi.rs:541-549`), `SpiPins.cs` niesie maskę CS; model karty na poziomie transferu jest wykonalny. Pozostałe ryzyko to DMA w `sdspi` (`SDSPI_DEFAULT_DMA`), które esp32sim deklaruje jako modelowane | Zmienić tryb z "pierwsza próba" na "ścieżka główna"; ryzyko zawęzić do DMA |
| 2.10 | 6, M0 | Macierz Pythona 3.11/3.13; lokalnie 3.14.6, czyli deweloper pracuje na wersji spoza CI | Dodać 3.14 do macierzy |
| 2.11 | M8 | "Go public when M1 to M3 pass" jest ostrzejsze niż specyfikacja ("private until a first piece works end to end", czyli M1) | Albo poprawić specyfikację, albo wrócić do M1; plan nie powinien po cichu zmieniać decyzji |
| 2.12 | 4.4, M2 | `unknown_reply` dla NMEA: plan nie mówi, co jest "własną odpowiedzią protokołu" na nieznany `$PMTK` (u MediaTeka `$PMTK001,<cmd>,0`) ani dla UBX | W M2 zdefiniować domyślne `Protocol` dla AT, PMTK i UBX z konkretnymi wartościami |

## 3. Ustalenia drobne

- 4.2: konsola idzie i jako `console` po gnieździe, i na stdout dla `DuplicateStdoutPopen`. Dwa
  właściciele tego samego strumienia; warto wybrać jeden i drugi zrobić opcjonalnym (`--console
  socket|stdout|both`).
- Ustalenie 4 (reset): `Machine::reboot()` zwraca `u32` (`esp-soc/src/machine.rs:306`), więc
  runner ma przyczynę resetu bez żadnej inferencji. Warto to zapisać, bo to wzmacnia amendment.
- M0: ROM ELF. Lokalnie jest wydanie `20241011`; nowsze to `20260313` i `20260528`. Przypiąć
  `20241011`, skoro na nim był robiony test ze specyfikacji.
- M1: `test_time_is_emulated` ("ten fixes in well under ten wall seconds"). Według #325 emulator
  chodzi 15-37x szybciej niż czas rzeczywisty, ale każda wymiana z peerem w Pythonie to blokada w
  czasie ściennym. Próg warto ustawić z marginesem (np. < 5 s), żeby test nie był flaky na CI.
- Sekcja 9: M0 + M1 w dwa tygodnie, z zimnym buildem fat-LTO, buildem firmware w Dockerze i
  runnerem w Rust. Wykonalne, ale ścieżka krytyczna to runner i jego test z fałszywym peerem; plan
  dobrze to stawia na początku.
- 4.3: `firmware_sha256` w nagłówku nagrania. Dobrze; dopisać, że hash dotyczy `app.bin`, nie
  całego obrazu flash, inaczej zmiana bootloadera unieważnia nagrania.

## 4. Weryfikacja faktów

Każde twierdzenie planu, które dało się sprawdzić, i wynik.

| Twierdzenie planu | Źródło | Wynik |
|---|---|---|
| `SERVICE_LIB_NAMES` to zamknięty słownik, `FIXTURES_SERVICES` decyduje o fixture'ach | `pytest_embedded/utils.py:20-40` | zgadza się |
| pytest-embedded 2.9.3 | PyPI | zgadza się (`requires_python >= 3.10`) |
| pytest-embedded-qemu buduje na `DuplicateStdoutPopen` | `pytest_embedded_qemu/qemu.py:100` | zgadza się |
| `--board` na esp32: `none, bare, esp32dev, ttgo-tdisplay, ai-thinker-cam` | `cli/src/lib.rs:551` | zgadza się |
| #325 otwarte, bez komentarzy | GitHub, 2026-10-29 | zgadza się (utworzone 16:42Z tego dnia) |
| `cli` jest też biblioteką `esp32sim` | `cli/Cargo.toml:8-9` | zgadza się |
| `esp32` eksportuje `machine`, `Machine`, `Stop` | `esp32/src/lib.rs:12-13` | zgadza się |
| `Machine`: `boot_rom`, `run`, `reboot`, `drain_console`, `max_cycles`, `console`, `load_input` | `esp-soc/src/machine.rs:79-91,301-336,506`; `cli/examples/uart_echo.rs:47-73` | zgadza się |
| `bus.board` i `attach_board_devices()` publiczne, użyte w `uart_echo.rs` | `esp32/src/bus.rs:61,413`; `uart_echo.rs:104-114` | zgadza się |
| Nie na crates.io | crates.io API | zgadza się |
| Fat LTO, jeden codegen unit | `Cargo.toml:10-14` | zgadza się |
| PR #322 otwarty, nie zmergowany | GitHub | zgadza się |
| IDF domyślnie `ESP32_REV_MIN_0` | `components/esp_hw_support/port/esp32/Kconfig.hw_support` (v5.5.5 lokalnie) | zgadza się |
| `pppos_client` w esp-protocols | `components/esp_modem/examples/pppos_client` | zgadza się |
| `uart_tx` strzela przy zapisie FIFO | `esp32/src/bus.rs:310` | zgadza się |
| RX FIFO 128 bajtów, `RXFIFO_OVF` | `esp-periph/src/uart.rs:9,13` | zgadza się |
| `--ble` przez trampolinę FreeRTOS, nie przez pin | `docs/esp32.md:64-66` | zgadza się |
| LACT sleep-time stepping niemodelowane | `docs/esp32.md:44` | zgadza się |
| `--realtime`, `--flash-at`, `--max-seconds`, `--wifi`, `--wifi-nearby`, `--net` istnieją | `cli/src/lib.rs:149-188` | zgadza się |
| `guest_reset` nie ma jeszcze w trait'cie | `esp-soc/src/board.rs` | zgadza się (brak) |
| ADC jest hookiem płytki | `esp-soc/src/board.rs` | **nie**: brak metody ADC (ustalenie 1.3) |
| `StationLink` publiczny | `esp-soc/src/wifi.rs:490`, `esp32/src/bus.rs` | zgadza się (plan mówił "do sprawdzenia") |
| Klasyczny SPI woła `spi_transfer_pins` | `esp32/src/spi.rs:541-549` | zgadza się |
| bumble 0.0.236 | PyPI | zgadza się |
| Obraz `espressif/idf:v5.3.6` | Docker Hub | istnieje (2026-09-11) |
| Python 3.14.6, cargo 1.93.0, IDF v5.5.5, Docker 24.0.2 | ta maszyna | zgadza się |
| ROM ELF `20241011/esp32_rev300_rom.elf` | `~/.espressif/tools/esp-rom-elfs/` | zgadza się |
| `mtools` brak, `/usr/sbin/pppd` jest, pytest-embedded niezainstalowany | ta maszyna | zgadza się |
| Przykłady IDF obecne (hello_world, bt_spp_initiator, sdspi, deep_sleep, controller_hci_uart_esp32) | `~/esp/esp-idf/examples` | zgadza się |
| "main at 2e36224 (v0.3.0)" | tagi esp32sim | **nieścisłe**: v0.3.0 = `964c26c`, main 227 commitów dalej |

## 5. Zgodność ze specyfikacją (issue #1)

- Reguła "anything the plan finds wrong gets amended there": plan trzyma ją w sekcji 1. Po tym
  review do specyfikacji trafiają dodatkowo: brak hooka ADC (1.3), `real` na esp32sim wymagający
  własnego projektu czasu (1.2), i decyzja o progu publikacji (2.11).
- "Never fork esp32sim as a whole": runner jako git-dep przypięty po rewizji spełnia to.
- "Models are processes in spirit": architektura i protokół to realizują; po poprawce 1.4 będzie
  to też wykonalne bez zakleszczeń.
- Harness API ze specyfikacji (`gnss.replay`, `console.expect`) zachowane bez zmian.
- Walidacja wyłącznie na publicznym firmware: zachowana, z zastrzeżeniem 1.1 (profil LilyGO nie
  jest potrzebny do żadnego testu walidacyjnego, więc tym bardziej nie powinien blokować M1).

## 6. Co zrobić z tym review

1. Poprawić w planie 1.1 do 1.5 (razem około 30 linii).
2. Dopisać do issue #1 trzy amendmenty z sekcji 5.
3. Przejść tabelę z sekcji 2 i zamknąć każdy wiersz jedną zmianą albo świadomym "nie".
4. Dopiero potem M0.
