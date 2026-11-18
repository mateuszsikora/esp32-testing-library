use super::*;

/// Every variant, so a misspelt name fails here. `Debug` starts with the variant's name; the
/// next character must end it, or `CostModelLifecycle` named `CostModel` would pass.
#[test]
fn stop_names_are_the_variant_names() {
    for stop in [
        Stop::MaxInsns,
        Stop::Halted,
        Stop::Breakpoint(0x4008_0000),
        Stop::Unimplemented(0x4008_0000, 0x12),
        Stop::SwReset,
        Stop::Simcall(1),
        Stop::Ebreak(0x4008_0000),
        Stop::Watch(0x3ffb_0000, 0, 1),
        Stop::Exceptions(3),
        Stop::CostModel {
            core: 0,
            pc: 0,
            reason: String::new(),
        },
        Stop::CostModelLifecycle {
            kind: emu_core::LifecycleKind::ChipReset,
            reason: String::new(),
        },
    ] {
        let debug = format!("{stop:?}");
        let name = stop_name(&stop);
        let rest = debug.strip_prefix(name);
        assert!(
            rest.is_some_and(|r| r.is_empty() || r.starts_with([' ', '('])),
            "{debug} named {name}"
        );
    }
}

/// The controller symbols `Ble::enable` takes, as esp32sim's own tests give them
/// (`esp32/src/ble.rs`), in IRAM and DRAM.
fn controller_elf() -> esp_soc::elf::Elf {
    let base = 0x4008_0000;
    let mut elf = esp_soc::elf::Elf::default();
    for (name, address) in [
        ("esp_bt_controller_init", base),
        ("xTaskCreatePinnedToCore", base + 0x400),
        ("vTaskDelay", base + 0x500),
        ("esp_bt_controller_enable", base + 0x610),
        ("esp_vhci_host_check_send_available", base + 0x620),
        ("esp_vhci_host_send_packet", base + 0x630),
        ("esp_vhci_host_register_callback", base + 0x640),
        ("_bt_controller_bss_start", 0x3ffc_7104),
        ("_bt_controller_bss_end", 0x3ffc_7c7c),
    ] {
        elf.by_name.insert(name.into(), address);
    }
    elf.symbol_sizes
        .insert("esp_bt_controller_init".into(), 0x400);
    elf
}

/// `enable_ble` puts a fresh session in, with esp32sim's virtual controller: the forwarder must
/// be the one left.
#[test]
fn forward_leaves_the_forwarder_as_the_guests_controller() {
    let mut m = esp32::machine(chip::esp32::MAC, 4 << 20);
    let hci = hci::Shared::default();
    m.forward_bt(&controller_elf(), hci.clone()).unwrap();
    assert!(!m.bus.ble.hooks.is_empty());
    m.bus.ble.session.advance_to(5);
    m.bus.ble.session.send(&[0x01, 0x03, 0x0c, 0x00]);
    assert_eq!(hci.borrow().tx, [(5, vec![0x01, 0x03, 0x0c, 0x00])]);
    // esp32sim's controller would have answered HCI_Reset
    assert_eq!(m.bus.ble.session.pop_packet(), None);
}

#[test]
fn forward_refuses_an_elf_without_the_controller() {
    let mut m = esp32::machine(chip::esp32::MAC, 4 << 20);
    assert_eq!(
        m.forward_bt(&esp_soc::elf::Elf::default(), hci::Shared::default()),
        Err("BLE requires ELF symbol esp_bt_controller_init".into())
    );
}

/// The P4's revision from the files `--flash-at` places: the header at 0x2000 inside an image of
/// the whole flash from 0x0, as PlatformIO's `firmware.factory.bin` is, decides as a bootloader
/// file of its own does; a later file over 0x2000 replaces it.
#[test]
fn the_p4_revision_is_read_at_the_bootloader_s_flash_address() {
    let dir = std::env::temp_dir().join(format!("esp32tl-p4rev-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let header = |min: u16| {
        let mut h = vec![0; 24];
        h[0] = 0xE9;
        h[12] = 0x12;
        h[15..17].copy_from_slice(&min.to_le_bytes());
        h
    };
    let mut factory = vec![0xff; 0x2000];
    factory.extend(header(301));
    factory.extend(vec![0xff; 0x100]);
    let (whole, boot) = (dir.join("factory.bin"), dir.join("bootloader.bin"));
    std::fs::write(&whole, &factory).unwrap();
    std::fs::write(&boot, header(100)).unwrap();
    let opts = |placed: &str| {
        cli::parse(
            format!("--chip esp32p4 {placed} --max-seconds 1")
                .split_whitespace()
                .map(String::from),
        )
        .unwrap()
    };
    let (w, b) = (whole.display(), boot.display());
    assert_eq!(p4_rev3(&opts(&format!("--flash-at 0x0={w}"))), Ok(true));
    assert_eq!(
        p4_rev3(&opts(&format!("--flash-at 0x0={w} --flash-at 0x2000={b}"))),
        Ok(false)
    );
    assert_eq!(
        p4_rev3(&opts(&format!("--flash-at 0x2000={b} --flash-at 0x0={w}"))),
        Ok(true)
    );
    std::fs::remove_dir_all(&dir).unwrap();
}
