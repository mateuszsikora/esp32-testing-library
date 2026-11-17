use super::*;

fn parse_str(line: &str) -> Result<Opts, Error> {
    parse(line.split_whitespace().map(String::from))
}

const MINIMAL: &str = "--chip esp32 --flash-at 0x1000=b.bin --max-seconds 3";

#[test]
fn minimal_command_line() {
    let opts = parse_str(MINIMAL).unwrap();
    assert_eq!(
        opts,
        Opts {
            chip: &crate::chip::esp32::PROFILE,
            rom: None,
            flash_at: vec![(0x1000, "b.bin".into())],
            flash_mb: 4,
            psram_mb: 0,
            max_seconds: 3.0,
            console: Console::Stdout,
            board: None,
            peer_timeout: Some(Duration::from_secs(60)),
            sd_card: None,
            wifi: None,
            realtime: false,
            flash_out: None,
            bt: None,
        }
    );
}

#[test]
fn flash_out_names_a_file_and_needs_no_board() {
    let opts = parse_str(&format!("{MINIMAL} --flash-out out/flash.bin")).unwrap();
    assert_eq!(opts.flash_out, Some("out/flash.bin".into()));
    assert_eq!(
        parse_str(&format!("{MINIMAL} --flash-out")),
        Err(Error::Usage("--flash-out needs a value".into()))
    );
}

#[test]
fn flash_at_offsets_are_hex_with_or_without_prefix() {
    let opts = parse_str("--chip ESP32 --max-seconds 0.5 --flash-at 0x1000=b.bin --flash-at 8000=p.bin --flash-at 0x10000=a=b.bin").unwrap();
    assert_eq!(
        opts.flash_at,
        vec![
            (0x1000, "b.bin".into()),
            (0x8000, "p.bin".into()),
            (0x10000, "a=b.bin".into())
        ]
    );
    assert_eq!(opts.max_seconds, 0.5);
}

#[test]
fn options() {
    let opts = parse_str(&format!(
        "{MINIMAL} --rom r.elf --flash-mb 8 --psram-mb 4 --console both --board socket:s"
    ))
    .unwrap();
    assert_eq!(opts.rom, Some("r.elf".into()));
    assert_eq!(
        (opts.flash_mb, opts.psram_mb, opts.console),
        (8, 4, Console::Both)
    );
}

#[test]
fn boards_and_peer_timeout() {
    let opts = parse_str(&format!(
        "{MINIMAL} --board socket:/tmp/b.sock --peer-timeout 2.5 --console socket"
    ))
    .unwrap();
    assert_eq!(opts.board, Some(Board::Socket("/tmp/b.sock".into())));
    assert_eq!(opts.board.unwrap().to_string(), "socket:/tmp/b.sock");
    assert_eq!(opts.peer_timeout, Some(Duration::from_millis(2500)));
    let opts = parse_str(&format!(
        "{MINIMAL} --board tcp:[::1]:7000 --peer-timeout 0"
    ))
    .unwrap();
    assert_eq!(opts.board, Some(Board::Tcp("[::1]:7000".into())));
    assert_eq!(opts.peer_timeout, None);
    let opts = parse_str(&format!("{MINIMAL} --board tcp:localhost:7000")).unwrap();
    assert_eq!(opts.board.unwrap().to_string(), "tcp:localhost:7000");
}

#[test]
fn an_sd_card_slot_takes_its_pins_in_any_order_and_a_card_from_power_on() {
    let opts = parse_str(&format!(
        "{MINIMAL} --sd-spi cs=13,miso=2,mosi=15,sclk=14 --sd-rw card.img"
    ))
    .unwrap();
    let wiring = Wiring {
        sclk: 14,
        mosi: 15,
        miso: 2,
        cs: 13,
    };
    assert_eq!(opts.sd_card, Some((Some("card.img".into()), wiring)));
    // without --sd-rw the slot is empty at power-on
    let opts = parse_str(&format!("{MINIMAL} --sd-spi sclk=14,mosi=15,miso=2,cs=13")).unwrap();
    assert_eq!(opts.sd_card, Some((None, wiring)));
    // MISO may be an input-only pin
    let opts = parse_str(&format!(
        "{MINIMAL} --sd-rw card.img --sd-spi sclk=18,mosi=23,miso=39,cs=5"
    ))
    .unwrap();
    assert_eq!(opts.sd_card.unwrap().1.miso, 39);
}

#[test]
fn sd_card_usage_errors() {
    for (args, message) in [
        (
            "--sd-rw card.img",
            "--sd-rw needs --sd-spi: the card's pins",
        ),
        (
            "--sd-rw c --sd-spi sclk=14,mosi=15,miso=2",
            "--sd-spi sclk=14,mosi=15,miso=2: expected sclk=N,mosi=N,miso=N,cs=N",
        ),
        (
            "--sd-rw c --sd-spi sclk=14,mosi=15,miso=2,cs",
            "--sd-spi sclk=14,mosi=15,miso=2,cs: expected sclk=N,mosi=N,miso=N,cs=N",
        ),
        (
            "--sd-rw c --sd-spi sclk=14,mosi=15,miso=2,cs=13,sclk=12",
            "--sd-spi sclk=14,mosi=15,miso=2,cs=13,sclk=12: sclk is given twice",
        ),
        (
            "--sd-rw c --sd-spi clk=14,mosi=15,miso=2,cs=13",
            "--sd-spi clk=14,mosi=15,miso=2,cs=13: \"clk\" is not sclk, mosi, miso or cs",
        ),
        (
            "--sd-rw c --sd-spi sclk=24,mosi=15,miso=2,cs=13",
            "--sd-spi sclk=24,mosi=15,miso=2,cs=13: \"24\" is not a GPIO of the classic ESP32",
        ),
        (
            "--sd-rw c --sd-spi sclk=14,mosi=15,miso=2,cs=36",
            "--sd-spi sclk=14,mosi=15,miso=2,cs=36: GPIO 36 is input only",
        ),
        (
            "--sd-rw c --sd-spi sclk=14,mosi=14,miso=2,cs=13",
            "--sd-spi sclk=14,mosi=14,miso=2,cs=13: the four pins must differ",
        ),
    ] {
        assert_eq!(
            parse_str(&format!("{MINIMAL} {args}")),
            Err(Error::Usage(message.into())),
            "{args}"
        );
    }
}

#[test]
fn help() {
    assert_eq!(parse_str("--chip esp32 --help"), Err(Error::Help));
}

#[test]
fn usage_errors() {
    for line in [
        "--flash-at 0x1000=b.bin --max-seconds 3",
        "--chip s3 --flash-at 0x1000=b.bin --max-seconds 3",
        "--chip esp32 --flash-at 0x1000=b.bin",
        "--chip esp32 --max-seconds 3",
        "--chip esp32 --flash-at 0x1000 --max-seconds 3",
        "--chip esp32 --flash-at 0xzz=b.bin --max-seconds 3",
        "--chip esp32 --flash-at 0x1000= --max-seconds 3",
        "--chip esp32 --flash-at 0x1000=b.bin --max-seconds 0",
        "--chip esp32 --flash-at 0x1000=b.bin --max-seconds inf",
        "--chip esp32 --flash-at 0x1000=b.bin --max-seconds 3 --psram-mb 2",
        "--chip esp32 --flash-at 0x1000=b.bin --max-seconds 3 --flash-mb 0",
        "--chip esp32 --flash-at 0x1000=b.bin --max-seconds 3 --flash-mb 3",
        "--chip esp32 --flash-at 0x1000=b.bin --max-seconds 3 --flash-mb 32",
        "--chip esp32 --flash-at 0x1000=b.bin --max-seconds 3 --flash-mb 17592186044416",
        "--chip esp32 --flash-at 0x1000=b.bin --max-seconds 3 --console uart0",
        "--chip esp32 --flash-at 0x1000=b.bin --max-seconds 3 --board none",
        "--chip esp32 --flash-at 0x1000=b.bin --max-seconds 3 --board socket:",
        "--chip esp32 --flash-at 0x1000=b.bin --max-seconds 3 --board tcp:host",
        "--chip esp32 --flash-at 0x1000=b.bin --max-seconds 3 --board tcp::7000",
        "--chip esp32 --flash-at 0x1000=b.bin --max-seconds 3 --board tcp:h:99999",
        "--chip esp32 --flash-at 0x1000=b.bin --max-seconds 3 --peer-timeout -1",
        "--chip esp32 --flash-at 0x1000=b.bin --max-seconds 3 --peer-timeout nan",
        "--chip esp32 --flash-at 0x1000=b.bin --max-seconds 3 --peer-timeout 1e20",
        "--chip esp32 --flash-at 0x1000=b.bin --max-seconds 3 --peer-timeout inf",
        "--chip esp32 --flash-at 0x1000=b.bin --max-seconds 3 --peer-timeout 1e-10",
        "--chip esp32 --flash-at 0x1000=b.bin --max-seconds 3 --console socket",
        "--chip esp32 --flash-at 0x1000=b.bin --max-seconds 3 --console both",
        "--chip esp32 --flash-at 0x1000=b.bin --max-seconds",
    ] {
        assert!(matches!(parse_str(line), Err(Error::Usage(_))), "{line}");
    }
}

#[test]
fn wifi_takes_esp32sims_access_point_and_the_networks_around() {
    let opts = parse_str(&format!(
        "{MINIMAL} --board socket:s --wifi ssid=home,psk=password1,chan=11 \
         --wifi-nearby ssid=cafe,chan=3,rssi=-70,secure=0 --wifi-nearby ssid=office"
    ))
    .unwrap();
    let wifi = opts.wifi.unwrap();
    let ap = wifi.access_point();
    assert_eq!(
        (ap.ssid.as_str(), ap.psk.as_deref(), ap.channel),
        ("home", Some("password1"), 11)
    );
    let nearby: Vec<(&str, u8, i8, bool)> = wifi
        .nearby()
        .iter()
        .map(|b| (b.ssid.as_str(), b.channel, b.rssi, b.secure))
        .collect();
    assert_eq!(nearby, [("cafe", 3, -70, false), ("office", 1, -60, true)]);
    // every key is optional, as in esp32sim
    let opts = parse_str(&format!("{MINIMAL} --board socket:s --wifi ssid=x")).unwrap();
    assert_eq!(opts.wifi.unwrap().access_point().channel, 6);
}

#[test]
fn wifi_refuses_what_esp32sim_refuses_and_what_needs_another_flag() {
    for (args, says) in [
        ("--wifi ssid=x", "needs --board"),
        (
            "--board socket:s --wifi-nearby ssid=x",
            "needs the access point of --wifi",
        ),
        (
            "--board socket:s --wifi ssid=x,chan=15",
            "--wifi: invalid WiFi channel",
        ),
        (
            "--board socket:s --wifi ssid=x,pass",
            "--wifi: invalid WiFi option",
        ),
        (
            "--board socket:s --wifi ssid=x,epoch=1700000000",
            "the network here is the peer's",
        ),
        (
            "--board socket:s --wifi ssid=x --wifi-nearby chan=2",
            "--wifi-nearby: a nearby network needs an ssid",
        ),
        (
            "--board socket:s --wifi ssid=x --wifi-nearby ssid=x",
            "the access point's SSID",
        ),
        (
            "--board socket:s --wifi ssid=x,bssid=02:00:00:00:00:01 --wifi-nearby ssid=y,bssid=02:00:00:00:00:01",
            "the access point's BSSID",
        ),
    ] {
        match parse_str(&format!("{MINIMAL} {args}")) {
            Err(Error::Usage(m)) => assert!(m.contains(says), "{args}: {m}"),
            other => panic!("{args}: {other:?}"),
        }
    }
}

#[test]
fn realtime_needs_a_board() {
    let args = |extra: &[&str]| {
        [
            "--chip",
            "esp32",
            "--max-seconds",
            "3",
            "--flash-at",
            "0x1000=b.bin",
        ]
        .iter()
        .chain(extra)
        .map(|s| s.to_string())
        .collect::<Vec<_>>()
    };
    assert_eq!(
        parse(args(&["--realtime"])),
        Err(Error::Usage(
            "--realtime paces a run for the peer's real device: it needs --board".into()
        ))
    );
    let opts = parse(args(&["--realtime", "--board", "socket:/tmp/b"])).unwrap();
    assert!(opts.realtime);
}

#[test]
fn bt_names_the_application_elf_and_needs_a_board() {
    assert_eq!(
        parse_str(&format!("{MINIMAL} --bt app.elf")),
        Err(Error::Usage(
            "--bt makes the peer the guest's Bluetooth controller: it needs --board".into()
        ))
    );
    assert_eq!(
        parse_str(&format!("{MINIMAL} --board socket:s --bt")),
        Err(Error::Usage("--bt needs a value".into()))
    );
    let opts = parse_str(&format!("{MINIMAL} --board socket:s --bt build/app.elf")).unwrap();
    assert_eq!(opts.bt, Some("build/app.elf".into()));
}

#[test]
fn wifi_station_parses_and_refuses_conflicting_modes() {
    let opts = parse_str(&format!(
        "{MINIMAL} --board socket:s --wifi-sta ssid=setup,psk=password1,mac=02:00:00:00:00:09"
    ))
    .unwrap();
    let sta = opts.wifi.unwrap().station().unwrap();
    assert_eq!(sta.ssid, "setup");
    assert_eq!(sta.psk.as_deref(), Some("password1"));
    assert_eq!(sta.mac, [2, 0, 0, 0, 0, 9]);
    for (args, why) in [
        ("--wifi-sta ssid=x", "needs --board"),
        (
            "--board socket:s --wifi-sta ssid=x --wifi ssid=y",
            "at once are not modelled",
        ),
        (
            "--board socket:s --wifi ssid=y --wifi-sta ssid=x",
            "at once are not modelled",
        ),
        (
            "--board socket:s --wifi-sta ssid=x --wifi-nearby ssid=y",
            "nearby networks need",
        ),
        (
            "--board socket:s --wifi-sta ssid=x --wifi-sta ssid=y",
            "given twice",
        ),
        (
            "--board socket:s --wifi-sta ssid=x,psk=short",
            "--wifi-sta: invalid station passphrase",
        ),
    ] {
        let err = parse_str(&format!("{MINIMAL} {args}")).unwrap_err();
        assert!(format!("{err:?}").contains(why), "{err:?}");
    }
}

#[test]
fn every_chip_esp32sim_models_is_taken_in_any_case() {
    for (name, chip) in [
        ("esp32", &crate::chip::esp32::PROFILE),
        ("ESP32S3", &crate::chip::esp32s3::PROFILE),
        ("esp32c3", &crate::chip::esp32c3::PROFILE),
        ("Esp32C6", &crate::chip::esp32c6::PROFILE),
    ] {
        let opts = parse_str(&format!(
            "--chip {name} --flash-at 0x0=b.bin --max-seconds 3"
        ))
        .unwrap();
        assert_eq!(opts.chip, chip);
    }
}

#[test]
fn a_chip_the_runner_does_not_emulate_is_named_with_those_it_does() {
    assert_eq!(
        parse_str("--chip ESP32P4 --flash-at 0x0=b.bin --max-seconds 3"),
        Err(Error::Usage(
            "--chip esp32p4: not one of esp32, esp32s3, esp32c3, esp32c6".into()
        ))
    );
    assert_eq!(
        parse_str("--flash-at 0x0=b.bin --max-seconds 3"),
        Err(Error::Usage(
            "--chip is required (esp32, esp32s3, esp32c3, esp32c6)".into()
        ))
    );
}

#[test]
fn what_esp32sim_lacks_on_a_chip_is_a_usage_error() {
    let base = "--flash-at 0x0=b.bin --max-seconds 3 --board socket:/tmp/l";
    for (args, message) in [
        (
            "--chip esp32s3 --psram-mb 3",
            "--psram-mb: the ESP32-S3 supports only 0, 2, 4 or 8 MiB, got \"3\"",
        ),
        (
            "--chip esp32c3 --psram-mb 2",
            "--psram-mb: the ESP32-C3 supports only 0 MiB, got \"2\"",
        ),
        (
            "--chip esp32 --psram-mb 2",
            "--psram-mb: the classic ESP32 supports only 0 or 4 MiB, got \"2\"",
        ),
        (
            "--chip esp32c6 --wifi-sta ssid=ap",
            "--wifi-sta: esp32c6 has no virtual station for a SoftAP in esp32sim",
        ),
    ] {
        assert_eq!(
            parse_str(&format!("{args} {base}")),
            Err(Error::Usage(message.into())),
            "{args}"
        );
    }
    let opts = parse_str(&format!(
        "--chip esp32s3 --psram-mb 8 --wifi ssid=ap --bt a.elf {base}"
    ))
    .unwrap();
    assert_eq!((opts.chip.name, opts.psram_mb), ("esp32s3", 8));
}

#[test]
fn the_sd_card_s_pins_are_the_chip_s() {
    let line = |chip: &str, pins: &str| {
        parse_str(&format!(
            "--chip {chip} --flash-at 0x0=b.bin --max-seconds 3 --sd-spi {pins}"
        ))
    };
    // GPIO 34 drives on the S3; the classic ESP32 has it input only
    let opts = line("esp32s3", "sclk=34,mosi=4,miso=5,cs=8").unwrap();
    assert_eq!(opts.sd_card.unwrap().1.sclk, 34);
    assert_eq!(
        line("esp32", "sclk=34,mosi=4,miso=5,cs=8"),
        Err(Error::Usage(
            "--sd-spi sclk=34,mosi=4,miso=5,cs=8: GPIO 34 is input only".into()
        ))
    );
    assert_eq!(
        line("esp32c3", "sclk=22,mosi=4,miso=5,cs=8"),
        Err(Error::Usage(
            "--sd-spi sclk=22,mosi=4,miso=5,cs=8: \"22\" is not a GPIO of the ESP32-C3".into()
        ))
    );
}
