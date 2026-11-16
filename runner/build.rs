fn main() {
    println!("cargo:rerun-if-changed=Cargo.lock");
    let lock = std::fs::read_to_string("Cargo.lock").unwrap();
    let source = lock
        .lines()
        .find(|line| line.contains("git+https://github.com/mikroverk/esp32sim"))
        .unwrap();
    let rev = source.split('#').nth(1).unwrap().trim_end_matches('"');
    println!("cargo:rustc-env=ESP32SIM_REV={rev}");
}
