//! `--parent-fd N`: the end of the process that started the runner (protocol §9).
//!
//! That process holds the write end of a pipe and the runner inherits its read end as `N`. Nothing
//! is written on it: a thread of the runner blocks reading it, and the read returns end of file
//! once every write end is closed, which the kernel does when the process holding it ends, however
//! it ends, `SIGKILL` included. The thread then sets the flag `SIGTERM` sets, so the run ends
//! within one slice, as for a signal, and the run loop, which finds `gone` set too, ends it as
//! with the peer gone, exit code 4, rather than with `stats`.
//!
//! Without it the runner finds such a peer gone only at its next read or write on the socket, and
//! a turn's window up to `B` may hold no event for a long time (the test's `board.wait` of an
//! hour, say), all of it at full speed.
use std::fs::File;
use std::io::{ErrorKind, Read};
use std::os::fd::FromRawFd;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

/// What [`crate::session::End::PeerGone`] says when the parent's pipe ended the run.
pub const GONE: &str = "end of file on --parent-fd: the process that started the runner has ended";

/// Starts the thread that watches `fd`: at end of file, or at a read that fails, it sets `gone`,
/// then `signal`. Fails when `fd` is not an open descriptor, or not a pipe: a file or `/dev/null`
/// would end the run at once, as if the parent had ended, and a terminal would lose its input.
pub fn watch(fd: i32, gone: Arc<AtomicBool>, signal: Arc<AtomicBool>) -> Result<(), String> {
    // `fstat` before the descriptor is taken: std's `metadata` needs a `File`, which owns it
    // SAFETY: `libc::stat` is plain integers, for which all zeroes is a valid value
    let mut stat = unsafe { std::mem::zeroed::<libc::stat>() };
    // SAFETY: `fstat` writes only into `stat`, which outlives the call, and fails with EBADF for a
    // descriptor that is not open, without touching it
    if unsafe { libc::fstat(fd, &mut stat) } != 0 {
        let e = std::io::Error::last_os_error();
        return Err(format!(
            "--parent-fd {fd}: not an open file descriptor: {e}"
        ));
    }
    if stat.st_mode & libc::S_IFMT != libc::S_IFIFO {
        return Err(format!(
            "--parent-fd {fd}: not a pipe; the process that starts the runner keeps the write \
             end of one"
        ));
    }
    // SAFETY: the descriptor is open and a pipe, and `--parent-fd` hands it to the runner, which
    // opens and closes it nowhere else.
    let mut pipe = unsafe { File::from_raw_fd(fd) };
    std::thread::Builder::new()
        .name("parent-fd".into())
        .spawn(move || {
            let mut chunk = [0u8; 256];
            loop {
                match pipe.read(&mut chunk) {
                    // what the parent writes, should it write, means nothing
                    Ok(n) if n > 0 => {}
                    Err(e) if e.kind() == ErrorKind::Interrupted => {}
                    _ => break,
                }
            }
            // `gone` first: the store of `signal` releases it, and `Session::run` loads `signal`
            // with acquire before it reads `gone`
            gone.store(true, Ordering::SeqCst);
            signal.store(true, Ordering::SeqCst);
        })
        .map(|_| ())
        .map_err(|e| format!("--parent-fd {fd}: cannot start the thread that reads it: {e}"))
}

// In a file of its own, so that `make docs-check` tells a test-only change by its path.
#[cfg(test)]
mod tests;
