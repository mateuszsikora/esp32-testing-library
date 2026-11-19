use super::*;
use std::os::fd::IntoRawFd;
use std::time::{Duration, Instant};

fn flags() -> (Arc<AtomicBool>, Arc<AtomicBool>) {
    (
        Arc::new(AtomicBool::new(false)),
        Arc::new(AtomicBool::new(false)),
    )
}

/// Waits up to a wall second for `flag`.
fn soon(flag: &AtomicBool) -> bool {
    let deadline = Instant::now() + Duration::from_secs(1);
    while Instant::now() < deadline {
        if flag.load(Ordering::SeqCst) {
            return true;
        }
        std::thread::sleep(Duration::from_millis(1));
    }
    false
}

#[test]
fn the_flags_stay_clear_while_the_write_end_is_open_and_are_set_once_it_closes() {
    let (reader, mut writer) = std::io::pipe().unwrap();
    let (gone, signal) = flags();
    watch(reader.into_raw_fd(), gone.clone(), signal.clone()).unwrap();
    // what the parent writes does not end the run
    std::io::Write::write_all(&mut writer, b"x").unwrap();
    std::thread::sleep(Duration::from_millis(50));
    assert!(!gone.load(Ordering::SeqCst) && !signal.load(Ordering::SeqCst));
    drop(writer);
    assert!(soon(&signal), "the signal flag is set at end of file");
    assert!(gone.load(Ordering::SeqCst));
}

#[test]
fn a_descriptor_that_is_not_open_is_refused() {
    // far above any descriptor the tests open: one closed here could be reused by another test
    // running meanwhile, and the watcher would read it
    let fd = 1 << 20;
    let (gone, signal) = flags();
    let error = watch(fd, gone.clone(), signal).unwrap_err();
    assert!(
        error.starts_with(&format!("--parent-fd {fd}: not an open file descriptor: ")),
        "{error}"
    );
    assert!(!gone.load(Ordering::SeqCst));
}

#[test]
fn a_descriptor_that_is_not_a_pipe_is_refused_and_left_open() {
    let file = File::open("/dev/null").unwrap();
    let fd = file.into_raw_fd();
    let (gone, signal) = flags();
    assert_eq!(
        watch(fd, gone.clone(), signal),
        Err(format!(
            "--parent-fd {fd}: not a pipe; the process that starts the runner keeps the write end \
             of one"
        ))
    );
    // still the test's to close: watch did not take it
    // SAFETY: `fd` is open, from `into_raw_fd` above, and nothing else owns it
    let file = unsafe { File::from_raw_fd(fd) };
    assert!(file.metadata().is_ok());
    assert!(!gone.load(Ordering::SeqCst));
}
