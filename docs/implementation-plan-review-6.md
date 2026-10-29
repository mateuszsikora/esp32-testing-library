# Szóste review planu implementacji esp32tl

Przegląd `docs/implementation-plan.md` (679 linii, zapis z 2026-10-29 21:57) po pięciu
poprzednich review. Sprawdzony tego samego dnia względem issue #1 (bez zmian), `mikroverk/esp32sim`
na `2e36224` (#325 nadal otwarte, bez komentarzy) i `espressif/pytest-embedded` (PyPI 2.9.3).

## Werdykt

**Plan jest gotowy. Zero nowych ustaleń. Kolejne review planu nie ma sensu; następne powinno
czytać kod.**

Rewizja zamyka wszystkie cztery ustalenia z piątego review zmianami w treści (sekcja 1). Jedyne
nowe twierdzenie rewizji, źródło stałej `CPU_HZ`, zgadza się ze źródłem (sekcja 2). Szukałem
dalszych szwów i nie znalazłem nic, co zmieniałoby kod, kolejność albo zależności.

Przebieg sześciu rund, dla porządku:

| Review | Blokujące | Średnie | Drobne |
|---|---|---|---|
| 1 | 5 | 12 | 6 |
| 2 | 2 | 0 | 6 |
| 3 | 0 | 3 | 3 |
| 4 | 0 | 2 | 3 |
| 5 | 0 | 1 | 3 |
| 6 | 0 | 0 | 0 |

Każde ustalenie z rund 1 do 5 zostało zamknięte zmianą w tekście; żadne nie zostało odrzucone.

## 1. Zamknięcie ustaleń z piątego review

| Ustalenie | Gdzie zamknięte | Sprawdzone |
|---|---|---|
| 4.1 Trzeci składnik `next_deadline` | 4.2 (linie 254-259: minimum z trzech, z uzasadnieniem przez `esp32/src/soc.rs:106-112` i `idle_budget`), M1 (przypadek testowy z gościem w WFI, linie 425-427) | tak |
| 4.2 Autoflash usługi `idf`, bez drugiego wgrania | M4 (linie 473-474: `IdfSerial._start`, `--skip-autoflash` zostaje dla iteracji) | tak |
| 4.3 Resztki "serial/esp" | Ustalenie 1 (`esp` i `idf`, `IdfDut`), drzewo w sekcji 3 (`serial.py` opakowuje `IdfDut`) | tak |
| 4.4 Przeliczenie ns ↔ cykle | 4.2, nowy punkt "Time units" (linie 263-269): wzór, stałość przez cały bieg, źródło stałej, obowiązek zapisania w `docs/protocol.md` | tak |

Deklaracja z sekcji 10 planu zgadza się ze stanem tekstu.

## 2. Weryfikacja nowych twierdzeń rewizji

| Twierdzenie | Źródło | Wynik |
|---|---|---|
| `CPU_HZ` pochodzi z `esp32::periph::CPU_HZ` | `esp32/src/periph.rs:18` (`pub const CPU_HZ: u64 = 240_000_000`), `esp32/src/soc.rs:22` (`const CPU_HZ: u64 = periph::CPU_HZ`), `esp-soc/src/soc.rs:51` (stała trait'u) | zgadza się |
| zdolności płytki cache'owane w `attach_board_devices`, włączane przez `uses_uart_pins` | `esp-soc/src/board.rs:55-56` ("Cached by `attach_board_devices`") | zgadza się |

## 3. Co zrobić

1. Start M0.
2. Następne review na `docs/protocol.md` (kontrakt 4.1 przepisany na dokument) i na teście
   fałszywego peera z M1, bo tam reguły 1, 2 i 4 oraz trzy składniki `next_deadline` po raz
   pierwszy staną się kodem. Plan jest już tylko mapą; błędy, które zostały, pokażą się w
   implementacji, nie w tekście.
