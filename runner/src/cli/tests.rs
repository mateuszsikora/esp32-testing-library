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
