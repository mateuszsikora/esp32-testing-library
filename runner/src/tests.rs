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

/// The symbols the S3's and the C3's `Ble::enable` takes, as esp32sim's own tests give them
/// (`esp32s3/src/ble.rs`, `esp32c3/src/ble.rs`), from `base` in IRAM.
fn le_controller_elf(base: u32) -> esp_soc::elf::Elf {
    let mut elf = esp_soc::elf::Elf::default();
    for (name, address) in [
        ("esp_bt_controller_init", base),
        ("xTaskCreatePinnedToCore", base + 0x400),
        ("vTaskDelay", base + 0x500),
        ("esp_bt_controller_enable", base + 0x610),
        ("esp_vhci_host_check_send_available", base + 0x620),
        ("esp_vhci_host_send_packet", base + 0x630),
        ("esp_vhci_host_register_callback", base + 0x640),
        ("_bt_controller_bss_start", 0x3fc9_0000),
        ("_bt_controller_bss_end", 0x3fc9_1000),
    ] {
        elf.by_name.insert(name.into(), address);
    }
    elf.symbol_sizes
        .insert("esp_bt_controller_init".into(), 0x400);
    elf
}

/// The symbols the C6's native `Ble::enable` takes, as esp32sim's own tests give them
/// (`esp32c6/src/ble.rs`), in flash, and an `esp_app_desc` of ESP-IDF v5.5.4: it takes v5.5.x
/// only (`supported_idf`).
fn c6_controller_elf() -> esp_soc::elf::Elf {
    const FLASH: u32 = 0x4200_0000;
    let mut elf = esp_soc::elf::Elf::default();
    for (i, name) in [
        "esp_bt_controller_enable",
        "hci_transport_host_callback_register",
        "r_ble_hci_trans_buf_alloc",
        "r_ble_hci_trans_buf_free",
        "ble_transport_to_ll_cmd_impl",
        "ble_transport_to_ll_acl_impl",
        "npl_freertos_funcs_init",
        "npl_freertos_funcs_get",
        "esp_register_npl_funcs",
        "npl_freertos_mempool_init",
        "nimble_port_get_dflt_eventq",
        "npl_freertos_eventq_init",
        "calloc",
        "malloc",
        "free",
        "r_os_mempool_init",
        "r_os_mbuf_pool_init",
        "r_os_msys_register",
        "r_os_msys_get_pkthdr",
        "r_os_mbuf_append",
        "r_os_mbuf_free_chain",
        "xTaskCreatePinnedToCore",
        "vTaskDelay",
    ]
    .into_iter()
    .enumerate()
    {
        elf.by_name
            .insert(name.into(), FLASH + 0x1000 + i as u32 * 4);
    }
    elf.by_name
        .insert("esp_bt_controller_init".into(), FLASH + 2);
    elf.symbol_sizes
        .insert("esp_bt_controller_init".into(), 0x400);
    // esp_app_desc_t: its magic, then the version string at 112 (`Elf::idf_version`)
    let desc = 0x3c00_0020;
    let mut data = vec![0; 256];
    data[..4].copy_from_slice(&0xabcd_5432u32.to_le_bytes());
    data[112..118].copy_from_slice(b"v5.5.4");
    elf.sections.push(esp_soc::elf::Section {
        name: ".flash.appdesc".into(),
        addr: desc,
        data,
        is_bss: false,
    });
    elf.by_name.insert("esp_app_desc".into(), desc);
    elf
}

/// From `forward_bt` on, esp32sim keeps its `[ble]` lines for the run loop, which prints them and
/// ends the run on one that says the controller did not start, rather than print them itself, and
/// keeps them so when the hooks follow another app: on each of the four chips that have esp32sim's
/// Bluetooth.
macro_rules! keeps_bt_lines_test {
    ($name:ident, $machine:ty, $elf:expr) => {
        #[test]
        fn $name() {
            let mut m = <$machine as chip::Target>::new(4 << 20);
            assert_eq!(session::Guest::take_bt_line(&mut m), None);
            m.forward_bt(&$elf, hci::Shared::default()).unwrap();
            m.bus
                .ble
                .lines
                .note("controller task creation failed: 0x3ffea240".into());
            assert_eq!(
                session::Guest::take_bt_line(&mut m).as_deref(),
                Some("controller task creation failed: 0x3ffea240")
            );
            assert_eq!(session::Guest::take_bt_line(&mut m), None);
            // the hooks of another app, or none, keep them kept
            for elf in [None, Some(&$elf)] {
                session::Guest::bt_hooks(&mut m, elf).unwrap();
                m.bus
                    .ble
                    .lines
                    .note("BLE cannot read trampoline memory".into());
                assert_eq!(
                    session::Guest::take_bt_line(&mut m).as_deref(),
                    Some("BLE cannot read trampoline memory")
                );
            }
        }
    };
}

keeps_bt_lines_test!(
    forward_keeps_esp32sims_bluetooth_lines_for_the_run_loop,
    ::esp32::Machine,
    controller_elf()
);
keeps_bt_lines_test!(
    on_the_s3_forward_keeps_esp32sims_bluetooth_lines_for_the_run_loop,
    ::esp32s3::Machine,
    le_controller_elf(0x4038_8000)
);
keeps_bt_lines_test!(
    on_the_c3_forward_keeps_esp32sims_bluetooth_lines_for_the_run_loop,
    ::esp32c3::Machine,
    le_controller_elf(0x4038_0000)
);
keeps_bt_lines_test!(
    on_the_c6_forward_keeps_esp32sims_bluetooth_lines_for_the_run_loop,
    ::esp32c6::Machine,
    c6_controller_elf()
);

/// esp32sim's lines for a controller it could not start, and only those.
#[test]
fn the_lines_of_a_controller_esp32sim_cannot_start_are_known_by_their_start() {
    for line in [
        "BLE controller init does not match the guest calling convention",
        "BLE controller init crosses a noncontiguous flash mapping",
        "BLE cannot read trampoline memory",
        "load: address 0x400d1234 not mapped",
        "load: address 0x3f800000: Prohibited",
        "controller task creation failed: 0x3ffea240",
        "BLE init is not mapped to flash",
        "BLE init crosses a noncontiguous flash mapping",
        "native initialization step 3 failed: 0x101",
        "native initialization step 11 returned invalid result 0x0",
    ] {
        assert!(chip::bt_controller_failed(line), "{line}");
    }
    for line in [
        "the virtual controller is LE-only: enabled in mode 2 (BR/EDR), its commands get Unknown HCI Command",
        "rejected invalid VHCI packet pointer, length or controller state",
        "rejected oversized or unmapped controller packet",
        "native packet exceeds 260 bytes; dropping",
        "native ACL append failed: 1",
        "native event allocation failed; retrying",
        "native receive callback failed: 1",
        // the C6's packet refusals begin as its init refusals do
        "BLE invalid packet pointer or length",
        "BLE invalid mbuf chain",
        "BLE incomplete ACL packet",
        "BLE inconsistent mbuf lengths",
        "BLE unmapped packet",
        "dropped=2",
        "t=0.100000s controller task creation failed: 0x1",
    ] {
        assert!(!chip::bt_controller_failed(line), "{line}");
    }
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

/// `controller_elf` with every hook `shift` bytes further, and its `esp_app_desc` at the start of
/// the classic chip's DROM, where ESP-IDF links it.
fn controller_elf_at(shift: u32) -> esp_soc::elf::Elf {
    let mut elf = controller_elf();
    for (name, address) in elf.by_name.iter_mut() {
        if name.starts_with("esp_") || name.starts_with('x') || name.starts_with('v') {
            *address += shift;
        }
    }
    elf.by_name.insert("esp_app_desc".into(), APP_DESC);
    elf
}

/// `esp_app_desc` on the classic chip: DROM's first segment starts 0x20 into its page.
const APP_DESC: u32 = 0x3f40_0020;

/// The bootloader's mapping of an app image at `page` of the flash, 64 KiB pages, with an
/// `esp_app_desc_t` whose `app_elf_sha256` is `sha256`: the PRO CPU's MMU entry of DROM's first
/// page, as esp32sim keeps it.
fn boot_app(m: &mut esp32::Machine, page: usize, sha256: [u8; 32]) {
    let at = page * 0x1_0000 + 0x20;
    m.bus.flash[at..at + 4].copy_from_slice(&0xabcd_5432u32.to_le_bytes());
    m.bus.flash[at + 144..at + 176].copy_from_slice(&sha256);
    m.bus.mmu[0][0] = page as u32;
}

fn images(elfs: Vec<esp_soc::elf::Elf>) -> hci::Images {
    hci::Images::new(
        elfs.into_iter()
            .enumerate()
            .map(|(i, elf)| hci::Image {
                path: format!("app{i}.elf").into(),
                sha256: [i as u8 + 1; 32],
                elf,
            })
            .collect(),
    )
}

/// The hooks of an app the bootloader started from another slot, built from the second ELF, are
/// that ELF's; the forwarder stays the guest's controller; an app built from none of them gets no
/// hooks, and one whose image records no SHA-256 keeps those it had.
#[test]
fn the_bluetooth_hooks_follow_the_app_the_bootloader_started() {
    let mut m = esp32::machine(chip::esp32::MAC, 4 << 20);
    let hci = hci::Shared::default();
    let (first, second) = (controller_elf_at(0), controller_elf_at(0x20));
    m.forward_bt(&first, hci.clone()).unwrap();
    let mut images = images(vec![first, second]);
    images.check(&mut m).unwrap();
    let hooks = |m: &esp32::Machine| {
        let mut hooks = m.bus.ble.hooks.clone();
        hooks.sort_unstable();
        hooks
    };
    let ours = hooks(&m);
    assert!(ours.contains(&0x4008_0000));
    // nothing mapped yet: the application's
    images.follow(&mut m);
    assert_eq!(hooks(&m), ours);
    assert_eq!(
        m.bus.last_fault, None,
        "the read leaves no fault for the guest"
    );
    boot_app(&mut m, 0x21, [2; 32]);
    images.follow(&mut m);
    let theirs = hooks(&m);
    assert!(theirs.contains(&0x4008_0020) && !theirs.contains(&0x4008_0000));
    m.bus.ble.session.advance_to(7);
    m.bus.ble.session.send(&[0x01, 0x03, 0x0c, 0x00]);
    assert_eq!(hci.borrow().tx, [(7, vec![0x01, 0x03, 0x0c, 0x00])]);
    boot_app(&mut m, 0x02, [1; 32]);
    images.follow(&mut m);
    assert_eq!(hooks(&m), ours);
    boot_app(&mut m, 0x31, [9; 32]);
    images.follow(&mut m);
    assert!(m.bus.ble.hooks.is_empty());
    boot_app(&mut m, 0x02, [0; 32]);
    images.follow(&mut m);
    assert!(m.bus.ble.hooks.is_empty());
}

/// The first app the bootloader starts is the application's, even when the application's ELF is
/// not byte for byte the file its image was built from: its hooks stay, as a single `--bt` always
/// kept them, and its SHA-256 is the application's from then on, beside the ELF's own; a later app
/// of another SHA-256 gets none.
#[test]
fn the_first_app_keeps_the_application_s_hooks_when_its_elf_is_not_the_image_s() {
    let mut m = esp32::machine(chip::esp32::MAC, 4 << 20);
    m.forward_bt(&controller_elf_at(0), hci::Shared::default())
        .unwrap();
    let mut images = images(vec![controller_elf_at(0)]);
    images.check(&mut m).unwrap();
    let ours = m.bus.ble.hooks.len();
    // an image that records no SHA-256 is no app's first
    boot_app(&mut m, 0x02, [0; 32]);
    images.follow(&mut m);
    boot_app(&mut m, 0x02, [7; 32]);
    images.follow(&mut m);
    assert_eq!(m.bus.ble.hooks.len(), ours);
    boot_app(&mut m, 0x21, [8; 32]);
    images.follow(&mut m);
    assert!(m.bus.ble.hooks.is_empty());
    boot_app(&mut m, 0x02, [7; 32]);
    images.follow(&mut m);
    assert_eq!(m.bus.ble.hooks.len(), ours);
    boot_app(&mut m, 0x21, [8; 32]);
    images.follow(&mut m);
    // the ELF's own SHA-256 is still the application's, as for a first app the bootloader only
    // checked before it started this one
    boot_app(&mut m, 0x02, [1; 32]);
    images.follow(&mut m);
    assert_eq!(m.bus.ble.hooks.len(), ours);
}

/// Hooks that change while esp32sim's trampoline is in the flash put back the code under it
/// first: the new `Ble` does not know it, and `reboot()` would never restore it.
#[test]
fn new_hooks_put_back_the_code_under_the_trampoline() {
    let mut m = esp32::machine(chip::esp32::MAC, 4 << 20);
    m.forward_bt(&controller_elf_at(0), hci::Shared::default())
        .unwrap();
    m.bus.flash[0x2_0000..0x2_0004].copy_from_slice(b"trmp");
    m.bus.ble.original_flash = Some((0x2_0000, b"code".to_vec()));
    crate::session::Guest::bt_hooks(&mut m, Some(&controller_elf_at(0x20))).unwrap();
    assert_eq!(&m.bus.flash[0x2_0000..0x2_0004], b"code");
    assert_eq!(m.bus.ble.original_flash, None);
}

#[test]
fn a_bt_elf_without_the_controller_is_refused_before_the_run() {
    let mut m = esp32::machine(chip::esp32::MAC, 4 << 20);
    m.forward_bt(&controller_elf_at(0), hci::Shared::default())
        .unwrap();
    let images = images(vec![controller_elf_at(0), esp_soc::elf::Elf::default()]);
    assert_eq!(
        images.check(&mut m),
        Err("--bt app1.elf: BLE requires ELF symbol esp_bt_controller_init".into())
    );
}

/// FIPS 180-4's examples, and the padding across a block's end.
#[test]
fn sha256_gives_the_digests_of_the_standard_s_examples() {
    let hex = |d: [u8; 32]| d.iter().map(|b| format!("{b:02x}")).collect::<String>();
    assert_eq!(
        hex(hci::sha256(b"")),
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    );
    assert_eq!(
        hex(hci::sha256(b"abc")),
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    );
    assert_eq!(
        hex(hci::sha256(
            b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq"
        )),
        "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1"
    );
    assert_eq!(
        hex(hci::sha256(&[b'a'; 1_000_000])),
        "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0"
    );
}
