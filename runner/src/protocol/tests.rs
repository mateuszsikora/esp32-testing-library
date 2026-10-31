use super::*;

#[test]
fn time_conversion_rounds_as_section_3_says() {
    // 240 MHz: 25/6 ns per cycle
    assert_eq!(ns(0), 0);
    assert_eq!(ns(1), 4);
    assert_eq!(ns(6), 25);
    assert_eq!(cycles(0), 0);
    assert_eq!(cycles(1), 1);
    assert_eq!(cycles(4), 1);
    assert_eq!(cycles(5), 2);
    assert_eq!(cycles(25), 6);
    // a cycle converted to ns and back is the same cycle
    for c in (0..10_000).chain([u64::MAX / 1000, 1 << 40]) {
        assert_eq!(cycles(ns(c)), c, "cycle {c}");
    }
    // beyond the 76 s where cycles × 10⁹ overflows 64 bits
    let c = 200 * esp32::periph::CPU_HZ;
    assert_eq!(ns(c), 200_000_000_000);
    assert_eq!(cycles(200_000_000_000), c);
    assert_eq!(ns(c + 1), 200_000_000_004);
}

#[test]
fn character_times() {
    assert_eq!(char_ns(1, 9600), 1_041_666);
    assert_eq!(char_ns(3, 9600), 3_125_000);
    assert_eq!(char_ns(1, 115_200), 86_805);
}

#[test]
fn base64_round_trips() {
    for (raw, text) in [
        (&b""[..], ""),
        (b"f", "Zg=="),
        (b"fo", "Zm8="),
        (b"foo", "Zm9v"),
        (b"AT\r", "QVQN"),
        (b"Hello world!\n", "SGVsbG8gd29ybGQhCg=="),
    ] {
        assert_eq!(base64(raw), text);
        assert_eq!(unbase64(text).unwrap(), raw);
    }
    let all: Vec<u8> = (0..=255).collect();
    assert_eq!(unbase64(&base64(&all)).unwrap(), all);
    for bad in ["Zg", "Zg=", "Z===", "Zg==Zg==", "Zm9v!A==", "Zm 9"] {
        assert!(unbase64(bad).is_err(), "{bad}");
    }
}

#[test]
fn parses_the_peer_messages() {
    assert_eq!(
        parse(
            r#"{"type": "hello", "version": 1, "chip": "esp32", "board": "esp32-devkitc", "watch": [4, 5]}"#
        ),
        Ok(Incoming::Hello {
            version: 1,
            chip: "esp32".into(),
            watch: vec![4, 5]
        })
    );
    assert_eq!(
        parse(r#"{"type":"hello","version":1,"chip":"esp32"}"#),
        Ok(Incoming::Hello {
            version: 1,
            chip: "esp32".into(),
            watch: vec![]
        })
    );
    assert_eq!(
        parse(
            r#"{"type": "uart_rx", "t": 2000000000, "pin": 5, "baud": 9600, "data": "QVQN", "extra": [1]}"#
        ),
        Ok(Incoming::UartRx {
            t: 2_000_000_000,
            pin: 5,
            baud: 9600,
            data: b"AT\r".to_vec()
        })
    );
    assert_eq!(
        parse(r#"{"type": "wait", "ack": 9, "quiet_until": 3000000000, "latency": 1000000}"#),
        Ok(Incoming::Wait {
            ack: 9,
            quiet_until: Some(3_000_000_000),
            latency: Some(1_000_000)
        })
    );
    assert_eq!(
        parse(r#"{"type": "wait", "ack": 0, "quiet_until": null}"#),
        Ok(Incoming::Wait {
            ack: 0,
            quiet_until: None,
            latency: None
        })
    );
    assert_eq!(parse(r#"{"type":"stop"}"#), Ok(Incoming::Stop));
    assert_eq!(
        parse(r#"{"type":"adc","t":0,"pin":34,"volts":1.25}"#),
        Ok(Incoming::NotYet("adc"))
    );
}

#[test]
fn refuses_malformed_messages() {
    for line in [
        "",
        "[1]",
        "{\"type\":\"wait\"",
        r#"{"t":0}"#,
        r#"{"type":7}"#,
        r#"{"type":"bogus"}"#,
        r#"{"type":"wait","ack":-1}"#,
        r#"{"type":"wait","ack":1.5}"#,
        r#"{"type":"wait","ack":0,"latency":-5}"#,
        r#"{"type":"wait","ack":0,"quiet_until":"soon"}"#,
        r#"{"type":"uart_rx","t":0,"pin":64,"baud":9600,"data":""}"#,
        r#"{"type":"uart_rx","t":0,"pin":5,"baud":-1,"data":""}"#,
        r#"{"type":"uart_rx","t":0,"pin":5,"baud":9600,"data":"Zg"}"#,
        r#"{"type":"uart_rx","t":0,"pin":5,"baud":9600}"#,
        r#"{"type":"hello","version":1,"chip":"esp32","watch":[4,"5"]}"#,
        r#"{"type":"hello","version":1,"chip":"esp32","watch":4}"#,
    ] {
        assert!(parse(line).is_err(), "{line}");
    }
}

#[test]
fn writes_the_runner_messages() {
    let route = UartRoute::new(1, 1 << 26, Some(27), Some(115_200));
    assert_eq!(
        uart_tx(7, 1_500_000_000, &route, b"AT\r"),
        r#"{"type":"uart_tx","seq":7,"t":1500000000,"port":1,"tx_pins":[26],"baud":115200,"data":"QVQN"}"#
    );
    assert_eq!(
        uart_cfg(3, 5, &UartRoute::new(2, 0, None, None)),
        r#"{"type":"uart_cfg","seq":3,"t":5,"port":2,"tx_pins":[],"rx_pin":null,"baud":null}"#
    );
    assert_eq!(
        reset(12, 4, 12),
        r#"{"type":"reset","seq":12,"t":4,"cause":12}"#
    );
    let w = Warn {
        t: 1,
        kind: WarnKind::UnroutedRx,
        port: None,
        pin: 5,
        baud: None,
        bytes: 72,
    };
    assert_eq!(
        warn(&w),
        r#"{"type":"warn","t":1,"kind":"unrouted_rx","pin":5,"bytes":72,"detail":"uart_rx on GPIO 5, which no UART receives on: 72 bytes dropped"}"#
    );
    let w = Warn {
        kind: WarnKind::FrmErr,
        port: Some(1),
        baud: None,
        ..w
    };
    assert!(warn(&w).contains(r#""port":1,"pin":5,"baud":null,"bytes":72"#));
    let mut s = Stats {
        turns: 13,
        ..Stats::default()
    };
    s.uart[1] = PortStats {
        watched: true,
        rx_bytes: 864,
        ..PortStats::default()
    };
    assert_eq!(
        stats(
            12,
            "emulator",
            &[("stop", "Exceptions"), ("detail", "Exceptions(3)")],
            &s
        ),
        r#"{"type":"stats","t":12,"end":"emulator","stop":"Exceptions","detail":"Exceptions(3)","resets":0,"turns":13,"uart":[{"port":1,"tx_bytes":0,"rx_bytes":864,"rxfifo_ovf":0,"frm_err":0}],"unrouted_rx_bytes":0,"inputs_on_output":0,"inputs_not_applied":0,"rx_bytes_not_paced":0}"#
    );
    for line in [hello(), sync(3)] {
        assert!(parse_json(&line).is_ok(), "{line}");
    }
}

#[test]
fn the_esp32sim_revision_is_the_one_cargo_builds() {
    let manifest = include_str!("../../Cargo.toml");
    let pins: Vec<&str> = manifest
        .lines()
        .filter(|l| l.contains("mikroverk/esp32sim") && !l.trim_start().starts_with('#'))
        .collect();
    assert!(!pins.is_empty());
    for line in pins {
        assert!(line.contains(&format!("rev = \"{ESP32SIM}\"")), "{line}");
    }
}
