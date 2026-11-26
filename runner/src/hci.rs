//! `--bt`: the guest's Bluetooth controller is the peer's. esp32sim's VHCI hooks hand the guest's
//! H4 packets to the session's boxed `HciController` (`esp-soc/src/ble.rs`, `ble/peer.rs`); the
//! [`Forwarder`] put there keeps each with the bus cycle it was sent at, for the run loop to send as
//! `hci_tx`, and hands the guest's host the packets of the peer's `hci_rx` that have come due, one
//! at each VHCI poll of the guest's controller task (protocol 5.22).
use esp_soc::ble::HciController;
use std::cell::RefCell;
use std::collections::VecDeque;
use std::rc::Rc;

#[derive(Default)]
pub struct Hci {
    /// What the guest's host sent since the run loop last took it, in order, with the bus cycle
    /// of each.
    pub tx: Vec<(u64, Vec<u8>)>,
    /// What has come due for the guest's host and it has not read yet, in order.
    pub rx: VecDeque<Vec<u8>>,
    /// The bus cycle the VHCI hooks last moved the controller to.
    cycles: u64,
}

/// What the forwarder in the machine and the run loop share.
pub type Shared = Rc<RefCell<Hci>>;

pub struct Forwarder(pub Shared);

impl HciController for Forwarder {
    /// esp32sim's `Send` hook moves the controller to the bus's cycle first (`intercept`,
    /// `esp-soc/src/ble/vhci.rs`), so the cycle is the packet's own.
    fn send_h4(&mut self, packet: &[u8]) {
        let mut hci = self.0.borrow_mut();
        let cycle = hci.cycles;
        hci.tx.push((cycle, packet.to_vec()));
    }

    fn poll_h4(&mut self) -> Option<Vec<u8>> {
        self.0.borrow_mut().rx.pop_front()
    }

    /// A chip reset, or the guest's `esp_bt_controller_deinit`: what the guest's host has not
    /// read is gone with it. What it sent before is not: it goes out as `hci_tx` before the
    /// `reset`.
    fn reset(&mut self) {
        self.0.borrow_mut().rx.clear();
    }

    fn advance_to(&mut self, cycles: u64) {
        self.0.borrow_mut().cycles = cycles;
    }

    /// The peer's packets come due at slice boundaries, not at a deadline of the controller's.
    fn next_deadline(&self) -> Option<u64> {
        None
    }
}

/// One `--bt` ELF: the file, the SHA-256 of its bytes, which the image built from it records as
/// its `esp_app_desc_t.app_elf_sha256`, and what esp32sim parsed of it.
pub struct Image {
    pub path: std::path::PathBuf,
    pub sha256: [u8; 32],
    pub elf: esp_soc::elf::Elf,
}

/// The `--bt` ELFs, the application's first, and which of them esp32sim's hooks are over: the
/// one of the application image the bootloader started. At every slice boundary the run loop
/// reads the `esp_app_desc_t` where the application's ELF has `esp_app_desc`, at the start of the
/// image's DROM, and, when its `app_elf_sha256` is not the one it read last, puts in the hooks of
/// the ELF with that SHA-256: an app booted from another slot than the application's, one an OTA
/// update wrote, gets its own controller hooks rather than ones over another image's addresses.
/// The first app the bootloader starts is the application's own image: when its SHA-256 is none of
/// the ELFs', the application's ELF is not byte for byte the file `elf2image` read (rebuilt, or
/// processed after), and that SHA-256 becomes another of the application's for the run, beside its
/// own, with a line on stderr, so that its hooks stay as a single `--bt` always kept them. Any later app built from none of them gets no
/// hooks, with a line on stderr; an image that records no SHA-256 (all zeros) keeps the hooks it
/// has. The guest maps its own descriptor for as long as it runs; the bootloader maps at the same
/// address each image it checks, so that, between a reset and its handing over, the hooks may
/// follow an image it checks and then does not start, before they follow the one it starts.
pub struct Images {
    pub images: Vec<Image>,
    /// Where the descriptor is: `esp_app_desc` of each ELF, without repeats.
    descs: Vec<u32>,
    /// Whether an app has been started: the first is the application's.
    started: bool,
    /// The SHA-256 of that first app, when it is none of the ELFs': the application's too.
    alias: Option<[u8; 32]>,
    /// The `app_elf_sha256` read last.
    seen: Option<[u8; 32]>,
    /// The image the hooks are over: the application's at first.
    hooked: Option<usize>,
}

impl Images {
    /// `images`, the first the one whose hooks are in place.
    pub fn new(images: Vec<Image>) -> Self {
        let mut descs: Vec<u32> = Vec::new();
        for image in &images {
            if let Some(&desc) = image.elf.by_name.get("esp_app_desc")
                && !descs.contains(&desc)
            {
                descs.push(desc);
            }
        }
        Self {
            images,
            descs,
            started: false,
            alias: None,
            seen: None,
            hooked: Some(0),
        }
    }

    /// At a slice boundary: the hooks of the app the guest runs, when it is not the one they are
    /// over.
    pub fn follow(&mut self, guest: &mut impl crate::session::Guest) {
        let Some(hash) = self.descs.iter().find_map(|&desc| guest.running_app(desc)) else {
            return;
        };
        if self.seen.replace(hash) == Some(hash) || hash == [0; 32] {
            return;
        }
        let mut found = self
            .images
            .iter()
            .position(|image| image.sha256 == hash)
            .or((self.alias == Some(hash)).then_some(0));
        if found.is_none() && !self.started {
            eprintln!(
                "esp32tl-runner: --bt {}: the app the bootloader started first was built from an \
                 ELF of SHA-256 {}, not this file's {}: its hooks stay over it, as the \
                 application's",
                self.images[0].path.display(),
                hex(&hash),
                hex(&self.images[0].sha256)
            );
            self.alias = Some(hash);
            found = Some(0);
        }
        self.started = true;
        if found == self.hooked {
            return;
        }
        let elf = found.map(|i| &self.images[i].elf);
        // each ELF took esp32sim's hooks before the run started (`check`)
        guest.bt_hooks(elf).expect("a --bt ELF takes the hooks");
        self.hooked = found;
        match found {
            Some(i) => eprintln!(
                "esp32tl-runner: Bluetooth controller hooks from {}, the app the bootloader started",
                self.images[i].path.display()
            ),
            None => eprintln!(
                "esp32tl-runner: the app the bootloader started was built from an ELF of SHA-256 \
                 {}, none of the --bt ELFs: it has no Bluetooth controller; pass its ELF as \
                 another --bt",
                hex(&hash)
            ),
        }
    }

    /// Before the run: every ELF but the application's takes esp32sim's hooks, which are then put
    /// back over the application's, so that `follow` cannot fail; the error names the ELF.
    pub fn check(&self, guest: &mut impl crate::session::Guest) -> Result<(), String> {
        for image in &self.images[1..] {
            guest
                .bt_hooks(Some(&image.elf))
                .map_err(|e| format!("--bt {}: {e}", image.path.display()))?;
        }
        if self.images.len() > 1 {
            guest.bt_hooks(Some(&self.images[0].elf))?;
        }
        Ok(())
    }
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// SHA-256 (FIPS 180-4) of `data`.
pub fn sha256(data: &[u8]) -> [u8; 32] {
    const K: [u32; 64] = [
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4,
        0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe,
        0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
        0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
        0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
        0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
        0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116,
        0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
        0xc67178f2,
    ];
    let mut h: [u32; 8] = [
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
        0x5be0cd19,
    ];
    let mut message = data.to_vec();
    message.push(0x80);
    while message.len() % 64 != 56 {
        message.push(0);
    }
    message.extend_from_slice(&(data.len() as u64 * 8).to_be_bytes());
    for block in message.as_chunks::<64>().0 {
        let mut w = [0u32; 64];
        for (i, word) in block.as_chunks::<4>().0.iter().enumerate() {
            w[i] = u32::from_be_bytes(*word);
        }
        for i in 16..64 {
            let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
            let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16]
                .wrapping_add(s0)
                .wrapping_add(w[i - 7])
                .wrapping_add(s1);
        }
        let mut v = h;
        for i in 0..64 {
            let s1 = v[4].rotate_right(6) ^ v[4].rotate_right(11) ^ v[4].rotate_right(25);
            let ch = (v[4] & v[5]) ^ (!v[4] & v[6]);
            let t1 = v[7]
                .wrapping_add(s1)
                .wrapping_add(ch)
                .wrapping_add(K[i])
                .wrapping_add(w[i]);
            let s0 = v[0].rotate_right(2) ^ v[0].rotate_right(13) ^ v[0].rotate_right(22);
            let maj = (v[0] & v[1]) ^ (v[0] & v[2]) ^ (v[1] & v[2]);
            let t2 = s0.wrapping_add(maj);
            v = [
                t1.wrapping_add(t2),
                v[0],
                v[1],
                v[2],
                v[3].wrapping_add(t1),
                v[4],
                v[5],
                v[6],
            ];
        }
        for (word, add) in h.iter_mut().zip(v) {
            *word = word.wrapping_add(add);
        }
    }
    let mut out = [0; 32];
    for (bytes, word) in out.as_chunks_mut::<4>().0.iter_mut().zip(h) {
        bytes.copy_from_slice(&word.to_be_bytes());
    }
    out
}

// In a file of its own, so that `make docs-check` tells a test-only change by its path.
#[cfg(test)]
mod tests;
