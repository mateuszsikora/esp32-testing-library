//! The socket to the peer (protocol §1): one connection, made once, NDJSON both ways.
//!
//! Reads and writes block for at most [`POLL`] at a time, so that a `SIGTERM` or `--peer-timeout`
//! ends a wait for the peer at once. A signal handler that only sets a flag would not: with
//! `SA_RESTART` a blocked `read` resumes after it, and the backend's teardown sends `SIGKILL`
//! after five wall seconds.
//!
//! Writes are buffered, and go out at every `sync`, when the buffer fills, and once a line has
//! waited [`LIVE`] in wall time, so a peer that reads the console or a tap between its turns sees
//! it as it comes. When the peer reads does not change what it reads: outside its turn it only
//! reads (protocol §4).
use crate::protocol::MAX_LINE;
use std::io::{ErrorKind, Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::os::unix::net::UnixStream;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

/// How long a read or write blocks before the flags are checked again.
const POLL: Duration = Duration::from_millis(20);
/// Writes are buffered up to this much.
const WRITE_BUFFER: usize = 64 << 10;
/// How long, in wall time, a queued line may wait before it is written out.
const LIVE: Duration = Duration::from_millis(20);
/// How long the last messages of a run may take to write, well under the five seconds after
/// which the backend's teardown sends `SIGKILL`.
const TEARDOWN: Duration = Duration::from_secs(2);

#[derive(Debug, PartialEq, Eq)]
pub enum LinkError {
    /// The peer is gone (exit code 4): end of file, a failed read or write, or the time limit.
    Gone(String),
    /// What the peer sent is not a line of the protocol (exit code 3).
    Protocol(String),
    /// `SIGTERM` or `SIGINT` arrived while the runner waited for the peer.
    Signal,
}

/// The runner's side of the protocol's transport.
pub trait Link {
    /// Queues one line, without its `\n`.
    fn send(&mut self, line: &str) -> Result<(), LinkError>;
    /// Writes out what [`Link::send`] queued.
    fn flush(&mut self) -> Result<(), LinkError>;
    /// Called between slices: writes out what has been queued for a while.
    fn idle(&mut self) -> Result<(), LinkError>;
    /// The run is ending: from now on a signal no longer interrupts a write, so that the last
    /// messages reach the peer, and writing them takes at most [`TEARDOWN`].
    fn closing(&mut self);
    /// After the last message: ends the connection (see [`SocketLink::close`]).
    fn close(&mut self);
    /// The next line from the peer, without its `\n`. `deadline` bounds the wait.
    fn recv(&mut self, deadline: Option<Instant>) -> Result<String, LinkError>;
    /// Waits until `until`, outside the peer's turn (protocol 12.2): a signal ends the wait, and so
    /// does the peer closing the socket. What the peer sends meanwhile waits for [`Link::recv`].
    fn wait_until(&mut self, until: Instant) -> Result<(), LinkError>;
}

enum Stream {
    Unix(UnixStream),
    Tcp(TcpStream),
}

impl Stream {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        match self {
            Stream::Unix(s) => s.read(buf),
            Stream::Tcp(s) => s.read(buf),
        }
    }
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        match self {
            Stream::Unix(s) => s.write(buf),
            Stream::Tcp(s) => s.write(buf),
        }
    }
}

pub struct SocketLink {
    stream: Stream,
    /// What has been read and not yet returned as a line.
    input: Vec<u8>,
    output: Vec<u8>,
    /// When the oldest line in `output` was queued.
    queued: Option<Instant>,
    /// How long a write may make no progress before the peer counts as gone; `None`: for ever.
    write_timeout: Option<Duration>,
    /// Set by [`Link::closing`]: when the last writes give up.
    teardown: Option<Instant>,
    /// How long the last writes get: [`TEARDOWN`], shorter in the tests.
    teardown_limit: Duration,
    signal: Arc<AtomicBool>,
}

impl SocketLink {
    pub fn unix(path: &std::path::Path, signal: Arc<AtomicBool>) -> std::io::Result<Self> {
        Self::new(Stream::Unix(UnixStream::connect(path)?), signal)
    }

    /// `timeout` bounds each connection attempt; without one the system's limit does, more than
    /// a minute. It does not bound the name lookup before them, which the resolver's own limit
    /// does; a signal ends the process then, the runner's handlers are not installed yet.
    pub fn tcp(
        address: &str,
        timeout: Option<Duration>,
        signal: Arc<AtomicBool>,
    ) -> std::io::Result<Self> {
        let mut last = None;
        for addr in address.to_socket_addrs()? {
            let stream = match timeout {
                Some(timeout) => TcpStream::connect_timeout(&addr, timeout),
                None => TcpStream::connect(addr),
            };
            match stream {
                Ok(stream) => {
                    stream.set_nodelay(true)?;
                    return Self::new(Stream::Tcp(stream), signal);
                }
                Err(e) => last = Some(e),
            }
        }
        Err(last.unwrap_or_else(|| {
            std::io::Error::new(ErrorKind::NotFound, "the name resolves to no address")
        }))
    }

    /// One end of a socket pair, for the tests.
    #[cfg(test)]
    pub fn pair(stream: UnixStream, signal: Arc<AtomicBool>) -> Self {
        Self::new(Stream::Unix(stream), signal).unwrap()
    }

    fn new(stream: Stream, signal: Arc<AtomicBool>) -> std::io::Result<Self> {
        match &stream {
            Stream::Unix(s) => {
                s.set_read_timeout(Some(POLL))?;
                s.set_write_timeout(Some(POLL))?;
            }
            Stream::Tcp(s) => {
                s.set_read_timeout(Some(POLL))?;
                s.set_write_timeout(Some(POLL))?;
            }
        }
        Ok(Self {
            stream,
            input: Vec::new(),
            output: Vec::new(),
            queued: None,
            write_timeout: None,
            teardown: None,
            teardown_limit: TEARDOWN,
            signal,
        })
    }

    /// `--peer-timeout` also bounds a write the peer does not read.
    pub fn set_write_timeout(&mut self, timeout: Option<Duration>) {
        self.write_timeout = timeout;
    }

    fn line(&mut self) -> Result<Option<String>, LinkError> {
        let Some(end) = self.input.iter().position(|&b| b == b'\n') else {
            if self.input.len() > MAX_LINE {
                return Err(LinkError::Protocol("a line longer than 1 MiB".into()));
            }
            return Ok(None);
        };
        if end > MAX_LINE {
            return Err(LinkError::Protocol("a line longer than 1 MiB".into()));
        }
        let rest = self.input.split_off(end + 1);
        let mut line = std::mem::replace(&mut self.input, rest);
        line.pop();
        String::from_utf8(line)
            .map(Some)
            .map_err(|_| LinkError::Protocol("a line that is not UTF-8".into()))
    }
}

impl SocketLink {
    /// Writes `output` from `written` on, counting what goes out there.
    fn write_from(&mut self, written: &mut usize) -> Result<(), LinkError> {
        // --peer-timeout counts from the last progress: a peer that reads slowly is still there
        let stalled =
            |timeout: Option<Duration>| timeout.and_then(|t| Instant::now().checked_add(t));
        let mut deadline = stalled(self.write_timeout);
        while *written < self.output.len() {
            match self.stream.write(&self.output[*written..]) {
                Ok(0) => return Err(LinkError::Gone("the socket accepts no more data".into())),
                Ok(n) => {
                    *written += n;
                    deadline = stalled(self.write_timeout);
                }
                Err(e) if e.kind() == ErrorKind::Interrupted => {}
                Err(e) if blocked(&e) => {
                    let now = Instant::now();
                    match self.teardown {
                        Some(teardown) if now >= teardown => {
                            return Err(LinkError::Gone(
                                "the peer did not read the last messages in time".into(),
                            ));
                        }
                        Some(_) => {}
                        None if self.signal.load(Ordering::Relaxed) => {
                            return Err(LinkError::Signal);
                        }
                        None => {}
                    }
                    if deadline.is_some_and(|d| now >= d) {
                        return Err(LinkError::Gone(
                            "the peer read nothing for longer than --peer-timeout".into(),
                        ));
                    }
                }
                Err(e) => return Err(LinkError::Gone(format!("write failed: {e}"))),
            }
        }
        Ok(())
    }
}

fn blocked(e: &std::io::Error) -> bool {
    matches!(e.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut)
}

impl Link for SocketLink {
    fn send(&mut self, line: &str) -> Result<(), LinkError> {
        self.output.extend_from_slice(line.as_bytes());
        self.output.push(b'\n');
        self.queued.get_or_insert_with(Instant::now);
        if self.output.len() >= WRITE_BUFFER {
            self.flush()?;
        }
        Ok(())
    }

    fn flush(&mut self) -> Result<(), LinkError> {
        let mut written = 0;
        let result = self.write_from(&mut written);
        // what went out stays out, whatever ended the write: the next flush goes on from there
        self.output.drain(..written);
        if self.output.is_empty() {
            self.queued = None;
        }
        result
    }

    fn idle(&mut self) -> Result<(), LinkError> {
        match self.queued {
            Some(since) if since.elapsed() >= LIVE => self.flush(),
            _ => Ok(()),
        }
    }

    fn closing(&mut self) {
        self.teardown = Some(Instant::now() + self.teardown_limit);
    }

    /// Shuts the write side, so the peer reads end of file after the last message, then reads
    /// and drops what the peer sent and the runner has not read: a run that ends in the peer's
    /// turn leaves the rest of that turn unread. On Linux a Unix socket closed with unread input
    /// resets the connection, and the peer loses what it had not read yet, `stats` among it.
    /// The peer writes nothing after its turn, so the drop stops at the first read that would
    /// block, or at end of file.
    fn close(&mut self) {
        let _ = match &self.stream {
            Stream::Unix(s) => s.shutdown(std::net::Shutdown::Write),
            Stream::Tcp(s) => s.shutdown(std::net::Shutdown::Write),
        };
        self.input.clear();
        let mut chunk = [0u8; 8192];
        loop {
            match self.stream.read(&mut chunk) {
                Ok(0) => break,
                Ok(_) => {}
                Err(e) if e.kind() == ErrorKind::Interrupted => {}
                Err(_) => break,
            }
        }
    }

    fn recv(&mut self, deadline: Option<Instant>) -> Result<String, LinkError> {
        let mut chunk = [0u8; 8192];
        loop {
            if let Some(line) = self.line()? {
                return Ok(line);
            }
            match self.stream.read(&mut chunk) {
                Ok(0) => return Err(LinkError::Gone("the peer closed the socket".into())),
                Ok(n) => self.input.extend_from_slice(&chunk[..n]),
                Err(e) if e.kind() == ErrorKind::Interrupted => {}
                Err(e) if blocked(&e) => {}
                Err(e) => return Err(LinkError::Gone(format!("read failed: {e}"))),
            }
            if self.signal.load(Ordering::Relaxed) {
                return Err(LinkError::Signal);
            }
            if deadline.is_some_and(|d| Instant::now() >= d) {
                return Err(LinkError::Gone(
                    "the peer's turn took longer than --peer-timeout".into(),
                ));
            }
        }
    }

    /// Reads in [`POLL`] steps while more than a step is left. The last stretch is a sleep: a
    /// read's timeout is rounded up to the kernel's tick, 4 ms at Linux's usual 250 Hz, and this
    /// wait is for at most a few milliseconds nearly every time. What has been queued for a while
    /// goes out meanwhile, as between slices.
    fn wait_until(&mut self, until: Instant) -> Result<(), LinkError> {
        let mut chunk = [0u8; 8192];
        loop {
            if self.signal.load(Ordering::Relaxed) {
                return Err(LinkError::Signal);
            }
            self.idle()?;
            let left = until.saturating_duration_since(Instant::now());
            if left <= POLL {
                std::thread::sleep(left);
                return Ok(());
            }
            match self.stream.read(&mut chunk) {
                Ok(0) => return Err(LinkError::Gone("the peer closed the socket".into())),
                Ok(n) => self.input.extend_from_slice(&chunk[..n]),
                Err(e) if e.kind() == ErrorKind::Interrupted => {}
                Err(e) if blocked(&e) => {}
                Err(e) => return Err(LinkError::Gone(format!("read failed: {e}"))),
            }
        }
    }
}

// In a file of its own, so that `make docs-check` tells a test-only change by its path.
#[cfg(test)]
mod tests;
