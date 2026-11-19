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
    assert_eq!(
        parse(r#"{"type": "gpio_in", "t": 3000000000, "levels": [[33, 1], [4, 0]]}"#),
        Ok(Incoming::GpioIn {
            t: 3_000_000_000,
            levels: vec![(33, true), (4, false)]
        })
    );
    assert_eq!(
        parse(r#"{"type": "release", "t": 3500000000, "pins": [33]}"#),
        Ok(Incoming::Release {
            t: 3_500_000_000,
            pins: vec![33]
        })
    );
    assert_eq!(
        parse(r#"{"type": "eth_rx", "t": 7, "data": "AAAAAAAAAAAAAAAAAAA="}"#),
        Ok(Incoming::EthRx {
            t: 7,
            data: vec![0; 14]
        })
    );
    assert_eq!(
        parse(r#"{"type": "hci_rx", "t": 7, "data": "BA4EAQMMAA=="}"#),
        Ok(Incoming::HciRx {
            t: 7,
            data: vec![0x04, 0x0e, 0x04, 0x01, 0x03, 0x0c, 0x00]
        })
    );
    assert_eq!(
        parse(r#"{"type": "console_rx", "t": 7, "data": "aGVscAo="}"#),
        Ok(Incoming::ConsoleRx {
            t: 7,
            data: b"help\n".to_vec()
        })
    );
    assert_eq!(
        parse(r#"{"type": "wifi_ap", "t": 8, "on": false}"#),
        Ok(Incoming::WifiAp { t: 8, on: false })
    );
    assert_eq!(
        parse(r#"{"type": "power", "t": 9, "on": true}"#),
        Ok(Incoming::Power { t: 9, on: true })
    );
    assert_eq!(
        parse(r#"{"type": "chip_reset", "t": 10}"#),
        Ok(Incoming::ChipReset { t: 10 })
    );
    assert_eq!(
        parse(
            r#"{"type":"sd_fault","t":9,"id":3,"op":"write","action":"delay","delay":5000000,"count":2,"blocks":[10,20]}"#
        ),
        Ok(Incoming::SdFault {
            t: 9,
            fault: Fault {
                id: 3,
                op: Op::Write,
                action: Action::Delay(5_000_000),
                blocks: Some((10, 20)),
                count: Some(2),
            }
        })
    );
    assert_eq!(
        parse(r#"{"type":"sd_fault","t":0,"id":0,"op":"read","action":"error","count":null}"#),
        Ok(Incoming::SdFault {
            t: 0,
            fault: Fault {
                id: 0,
                op: Op::Read,
                action: Action::Error,
                blocks: None,
                count: None,
            }
        })
    );
    assert_eq!(
        parse(r#"{"type":"sd_fault","t":2,"id":5,"op":"erase","action":"observe","delay":null}"#),
        Ok(Incoming::SdFault {
            t: 2,
            fault: Fault {
                id: 5,
                op: Op::Erase,
                action: Action::Observe,
                blocks: None,
                count: None,
            }
        })
    );
    assert_eq!(
        parse(r#"{"type":"sd_clear","t":4,"ids":[1,2]}"#),
        Ok(Incoming::SdClear {
            t: 4,
            ids: Some(vec![1, 2])
        })
    );
    assert_eq!(
        parse(r#"{"type":"sd_clear","t":4}"#),
        Ok(Incoming::SdClear { t: 4, ids: None })
    );
    assert_eq!(
        parse(r#"{"type":"sd_card","t":9,"image":"/tmp/card.img"}"#),
        Ok(Incoming::SdCard {
            t: 9,
            image: Some("/tmp/card.img".into())
        })
    );
    // null and absent alike: the card out
    for line in [
        r#"{"type":"sd_card","t":9,"image":null}"#,
        r#"{"type":"sd_card","t":9}"#,
    ] {
        assert_eq!(parse(line), Ok(Incoming::SdCard { t: 9, image: None }));
    }
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
        r#"{"type":"gpio_in","t":0}"#,
        r#"{"type":"gpio_in","t":0,"levels":null}"#,
        r#"{"type":"gpio_in","t":0,"levels":[33,1]}"#,
        r#"{"type":"gpio_in","t":0,"levels":[[33,2]]}"#,
        r#"{"type":"gpio_in","t":0,"levels":[[33,true]]}"#,
        r#"{"type":"gpio_in","t":0,"levels":[[33,1,0]]}"#,
        r#"{"type":"gpio_in","t":0,"levels":[[64,1]]}"#,
        r#"{"type":"gpio_in","t":-1,"levels":[[33,1]]}"#,
        r#"{"type":"release","pins":[33]}"#,
        r#"{"type":"release","t":0,"pins":[-1]}"#,
        r#"{"type":"eth_rx","t":0,"data":"AAAAAAAAAAAAAAAAAA=="}"#,
        r#"{"type":"eth_rx","data":"AAAAAAAAAAAAAAAAAAA="}"#,
        r#"{"type":"hci_rx","t":0,"data":""}"#,
        r#"{"type":"hci_rx","t":0,"data":"Zg"}"#,
        r#"{"type":"hci_rx","data":"BA=="}"#,
        r#"{"type":"console_rx","t":0,"data":"Zg"}"#,
        r#"{"type":"console_rx","data":"aGVscAo="}"#,
        r#"{"type":"wifi_ap","t":0,"on":1}"#,
        r#"{"type":"wifi_ap","t":0}"#,
        r#"{"type":"power","t":0}"#,
        r#"{"type":"power","t":0,"on":"off"}"#,
        r#"{"type":"power","on":false}"#,
        r#"{"type":"chip_reset"}"#,
        r#"{"type":"chip_reset","t":-1}"#,
        r#"{"type":"sd_card","image":"a.img"}"#,
        r#"{"type":"sd_card","t":0,"image":7}"#,
    ] {
        assert!(parse(line).is_err(), "{line}");
    }
}

#[test]
fn refuses_an_sd_fault_that_cannot_be() {
    let fault = |rest: &str| format!(r#"{{"type":"sd_fault","t":0,"id":1,{rest}}}"#);
    for (rest, error) in [
        (
            r#""op":"trim","action":"error""#,
            "\"op\" \"trim\" is not read, write or erase",
        ),
        (
            r#""op":"read","action":"drop""#,
            "\"action\" \"drop\" is not error, delay or observe",
        ),
        (
            r#""op":"write","action":"observe","delay":5"#,
            "an observe takes no \"delay\"",
        ),
        (
            r#""op":"erase","action":"error""#,
            "an erase cannot fail, only be delayed",
        ),
        (
            r#""op":"read","action":"error","delay":5"#,
            "an error takes no \"delay\"",
        ),
        (r#""op":"read","action":"delay""#, "a delay takes \"delay\""),
        (
            r#""op":"read","action":"delay","delay":0"#,
            "a delay of 0 ns; one is 1 ns to 10000000000 ns (10 s)",
        ),
        (
            r#""op":"read","action":"delay","delay":10000000001"#,
            "a delay of 10000000001 ns",
        ),
        (
            r#""op":"read","action":"error","count":0"#,
            "a \"count\" of 0",
        ),
        (
            r#""op":"read","action":"error","blocks":[4,3]"#,
            "\"blocks\" 4 to 3: the last before the first",
        ),
        (
            r#""op":"read","action":"error","blocks":[4]"#,
            "\"blocks\" is not [first, last]",
        ),
        (
            r#""op":"read","action":"error","blocks":[-1,3]"#,
            "holds something other than non-negative integers",
        ),
    ] {
        let line = fault(rest);
        let found = parse(&line).unwrap_err();
        assert!(found.contains(error), "{line}: {found}");
    }
    assert!(parse(r#"{"type":"sd_fault","t":0,"op":"read","action":"error"}"#).is_err());
    assert!(parse(r#"{"type":"sd_clear","t":0,"ids":[true]}"#).is_err());
}

#[test]
fn writes_sd_op() {
    let mut f = Faulted {
        cycle: 0,
        fault: 2,
        op: Op::Write,
        blocks: (7, 7),
        action: Action::Error,
        written: false,
    };
    assert_eq!(
        sd_op(5, &f),
        r#"{"type":"sd_op","t":5,"fault":2,"op":"write","blocks":[7,7],"action":"error","delay":null,"written":false}"#
    );
    f.op = Op::Erase;
    f.blocks = (8, 15);
    f.action = Action::Delay(1_000);
    f.written = true;
    assert_eq!(
        sd_op(6, &f),
        r#"{"type":"sd_op","t":6,"fault":2,"op":"erase","blocks":[8,15],"action":"delay","delay":1000,"written":true}"#
    );
    f.op = Op::Read;
    f.blocks = (9, 9);
    f.action = Action::Observe;
    f.written = false;
    assert_eq!(
        sd_op(7, &f),
        r#"{"type":"sd_op","t":7,"fault":2,"op":"read","blocks":[9,9],"action":"observe","delay":null,"written":false}"#
    );
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
        gpio_out(9, 2_000_000_000, &[(4, true)], 1 << 4, 1 << 4),
        r#"{"type":"gpio_out","seq":9,"t":2000000000,"changes":[[4,1]],"enabled":[4],"output":[4]}"#
    );
    assert_eq!(
        gpio_out(10, 3, &[], 0, 1 << 4),
        r#"{"type":"gpio_out","seq":10,"t":3,"changes":[],"enabled":[],"output":[4]}"#
    );
    assert_eq!(
        reset(12, 4, 12, 0x8004_0004),
        r#"{"type":"reset","seq":12,"t":4,"cause":12,"hint":2147745796}"#
    );
    assert_eq!(
        sd_card_ack(5, Some("/tmp/a \"b\".img"), None),
        r#"{"type":"sd_card_ack","t":5,"image":"/tmp/a \"b\".img","error":null}"#
    );
    assert_eq!(
        sd_card_ack(6, None, Some("x.img: No such file")),
        r#"{"type":"sd_card_ack","t":6,"image":null,"error":"x.img: No such file"}"#
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
    let w = Warn {
        kind: WarnKind::InputOnOutput,
        port: None,
        pin: 33,
        bytes: 0,
        ..w
    };
    assert_eq!(
        warn(&w),
        r#"{"type":"warn","t":1,"kind":"input_on_output","pin":33,"detail":"gpio_in on GPIO 33, which the guest drives as an output: the level is applied all the same"}"#
    );
    let mut s = Stats {
        turns: 13,
        inputs_on_output: 2,
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
        r#"{"type":"stats","t":12,"end":"emulator","stop":"Exceptions","detail":"Exceptions(3)","resets":0,"turns":13,"uart":[{"port":1,"tx_bytes":0,"rx_bytes":864,"rxfifo_ovf":0,"frm_err":0}],"unrouted_rx_bytes":0,"inputs_on_output":2,"inputs_not_applied":0,"rx_bytes_not_paced":0}"#
    );
    assert_eq!(
        eth_tx(4, 5, &[1, 2, 3]),
        r#"{"type":"eth_tx","seq":4,"t":5,"data":"AQID"}"#
    );
    s.wifi = Some(WifiStats {
        eth_tx: 1,
        eth_tx_dropped: 2,
        eth_rx: 3,
        eth_rx_dropped: 4,
        rx_dropped: 5,
    });
    assert!(stats(12, "stop", &[], &s).ends_with(
        r#""rx_bytes_not_paced":0,"wifi":{"eth_tx":1,"eth_tx_dropped":2,"eth_rx":3,"eth_rx_dropped":4,"rx_dropped":5}}"#
    ));
    // with --console-port usb only, after the others
    s.wifi = None;
    s.console_rx_lost = Some(6);
    assert!(stats(12, "stop", &[], &s).ends_with(r#""rx_bytes_not_paced":0,"console_rx_lost":6}"#));
    // a 1519-byte frame is over the limit; 1518 is not
    let long = format!(
        r#"{{"type":"eth_rx","t":0,"data":"{}"}}"#,
        base64(&[0; 1519])
    );
    assert!(parse(&long).unwrap_err().contains("1519 bytes"));
    let longest = format!(
        r#"{{"type":"eth_rx","t":0,"data":"{}"}}"#,
        base64(&[0; 1518])
    );
    assert!(parse(&longest).is_ok());
    for line in [hello(CLOCK), sync(3)] {
        assert!(parse_json(&line).is_ok(), "{line}");
    }
}

#[test]
fn hci_packets_both_ways_and_the_limits_of_the_vhci() {
    // HCI_Reset, as the guest's host sends it first
    assert_eq!(
        hci_tx(4, 5, &[0x01, 0x03, 0x0c, 0x00]),
        r#"{"type":"hci_tx","seq":4,"t":5,"data":"AQMMAA=="}"#
    );
    let hci_rx = |len: usize| {
        format!(
            r#"{{"type":"hci_rx","t":0,"data":"{}"}}"#,
            base64(&vec![2; len])
        )
    };
    assert_eq!(
        parse(&hci_rx(0)),
        Err("hci_rx: an H4 packet of 0 bytes; esp32sim's VHCI carries 1 to 496".into())
    );
    assert!(parse(&hci_rx(497)).unwrap_err().contains("497 bytes"));
    for len in [1, HCI_MAX] {
        assert_eq!(
            parse(&hci_rx(len)),
            Ok(Incoming::HciRx {
                t: 0,
                data: vec![2; len]
            })
        );
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

#[test]
fn flash_dump_and_its_ack() {
    assert_eq!(parse(r#"{"type":"flash_dump"}"#), Ok(Incoming::FlashDump));
    assert_eq!(
        flash_dump_ack(7, None),
        r#"{"type":"flash_dump_ack","t":7,"error":null}"#
    );
    assert_eq!(
        flash_dump_ack(7, Some(r#"a "b": no space"#)),
        r#"{"type":"flash_dump_ack","t":7,"error":"a \"b\": no space"}"#
    );
}
