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
        "{MINIMAL} --rom r.elf --flash-mb 8 --psram-mb 4 --console both"
    ))
    .unwrap();
    assert_eq!(opts.rom, Some("r.elf".into()));
    assert_eq!(
        (opts.flash_mb, opts.psram_mb, opts.console),
        (8, 4, Console::Both)
    );
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
        "--chip esp32 --flash-at 0x1000=b.bin --max-seconds",
    ] {
        assert!(matches!(parse_str(line), Err(Error::Usage(_))), "{line}");
    }
}
