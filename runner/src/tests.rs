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
    let mut m = esp32::machine(MAC, 4 << 20);
    let hci = hci::Shared::default();
    forward(&mut m, &controller_elf(), hci.clone()).unwrap();
    assert!(!m.bus.ble.hooks.is_empty());
    m.bus.ble.session.advance_to(5);
    m.bus.ble.session.send(&[0x01, 0x03, 0x0c, 0x00]);
    assert_eq!(hci.borrow().tx, [(5, vec![0x01, 0x03, 0x0c, 0x00])]);
    // esp32sim's controller would have answered HCI_Reset
    assert_eq!(m.bus.ble.session.pop_packet(), None);
}

#[test]
fn forward_refuses_an_elf_without_the_controller() {
    let mut m = esp32::machine(MAC, 4 << 20);
    assert_eq!(
        forward(
            &mut m,
            &esp_soc::elf::Elf::default(),
            hci::Shared::default()
        ),
        Err("BLE requires ELF symbol esp_bt_controller_init".into())
    );
}
