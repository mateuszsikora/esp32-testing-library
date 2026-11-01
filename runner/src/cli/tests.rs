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
            rom: None,
            flash_at: vec![(0x1000, "b.bin".into())],
            flash_mb: 4,
            psram_mb: 0,
            max_seconds: 3.0,
            console: Console::Stdout,
            board: None,
            peer_timeout: Some(Duration::from_secs(60)),
            sd_card: None,
        }
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
fn an_sd_card_takes_its_image_and_its_pins_in_any_order() {
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
    assert_eq!(opts.sd_card, Some(("card.img".into(), wiring)));
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
            "--sd-spi sclk=14,mosi=15,miso=2,cs=13",
            "--sd-spi needs --sd-rw: the card's image",
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
            "--sd-spi sclk=24,mosi=15,miso=2,cs=13: \"24\" is not a GPIO of the ESP32",
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
