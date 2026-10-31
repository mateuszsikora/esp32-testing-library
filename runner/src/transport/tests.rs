use super::*;
use std::io::BufRead;

fn link() -> (SocketLink, UnixStream, Arc<AtomicBool>) {
    let (ours, theirs) = UnixStream::pair().unwrap();
    let signal = Arc::new(AtomicBool::new(false));
    (SocketLink::pair(ours, signal.clone()), theirs, signal)
}

#[test]
fn lines_both_ways() {
    let (mut link, mut peer, _) = link();
    link.send("{\"a\":1}").unwrap();
    link.send("{\"b\":2}").unwrap();
    link.flush().unwrap();
    let mut reader = std::io::BufReader::new(peer.try_clone().unwrap());
    let mut line = String::new();
    reader.read_line(&mut line).unwrap();
    reader.read_line(&mut line).unwrap();
    assert_eq!(line, "{\"a\":1}\n{\"b\":2}\n");
    // two lines in one write, then one in two
    peer.write_all(b"one\ntw").unwrap();
    assert_eq!(link.recv(None).unwrap(), "one");
    peer.write_all(b"o\n").unwrap();
    assert_eq!(link.recv(None).unwrap(), "two");
    drop(reader);
    drop(peer);
    assert!(matches!(link.recv(None), Err(LinkError::Gone(_))));
}

#[test]
fn a_line_over_1_mib_is_a_protocol_error() {
    let (mut link, mut peer, _) = link();
    let writer = std::thread::spawn(move || {
        let _ = peer.write_all(&vec![b'x'; MAX_LINE + 2]);
        peer
    });
    assert_eq!(
        link.recv(None),
        Err(LinkError::Protocol("a line longer than 1 MiB".into()))
    );
    drop(link);
    writer.join().unwrap();
}

#[test]
fn the_peer_timeout_ends_a_blocked_read() {
    let (mut link, _peer, _) = link();
    let start = Instant::now();
    let result = link.recv(Some(start + Duration::from_millis(50)));
    assert!(matches!(result, Err(LinkError::Gone(m)) if m.contains("--peer-timeout")));
    assert!(start.elapsed() < Duration::from_secs(1));
}

#[test]
fn a_signal_ends_a_blocked_read_at_once() {
    let (mut link, _peer, signal) = link();
    let raiser = std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(30));
        signal.store(true, Ordering::Relaxed);
    });
    let start = Instant::now();
    assert_eq!(link.recv(None), Err(LinkError::Signal));
    assert!(start.elapsed() < Duration::from_secs(1));
    raiser.join().unwrap();
}

#[test]
fn a_peer_that_reads_nothing_times_out_a_write() {
    let (mut link, _peer, _) = link();
    link.set_write_timeout(Some(Duration::from_millis(50)));
    let line = "x".repeat(1 << 16);
    let result = (0..1000).try_for_each(|_| link.send(&line));
    assert!(matches!(result, Err(LinkError::Gone(m)) if m.contains("--peer-timeout")));
}

/// Reads everything the peer end gets until the runner's end closes.
fn read_all(mut peer: UnixStream) -> std::thread::JoinHandle<Vec<u8>> {
    std::thread::spawn(move || {
        let mut all = Vec::new();
        peer.read_to_end(&mut all).unwrap();
        all
    })
}

#[test]
fn a_write_cut_short_by_a_signal_goes_on_where_it_stopped_once_closing() {
    // the peer reads nothing until the signal: the socket buffer fills, and the write stops
    // part way. After closing() the rest goes out, once, and stats after it.
    let (mut link, peer, signal) = link();
    let line = "x".repeat(1 << 20);
    signal.store(true, Ordering::Relaxed);
    assert_eq!(link.send(&line), Err(LinkError::Signal));
    link.closing();
    let reader = read_all(peer);
    link.send("STATS").unwrap();
    link.flush().unwrap();
    drop(link);
    let all = reader.join().unwrap();
    assert_eq!(all.len(), line.len() + 1 + "STATS\n".len());
    assert!(all.ends_with(b"x\nSTATS\n"));
}

#[test]
fn closing_gives_up_on_a_peer_that_never_reads() {
    let (mut link, _peer, signal) = link();
    let limit = Duration::from_millis(200);
    link.teardown_limit = limit;
    signal.store(true, Ordering::Relaxed);
    link.closing();
    let start = Instant::now();
    let result = link.send(&"x".repeat(4 << 20));
    assert!(matches!(result, Err(LinkError::Gone(m)) if m.contains("last messages")));
    let took = start.elapsed();
    assert!(
        took >= limit && took < limit + Duration::from_secs(1),
        "{took:?}"
    );
}

#[test]
fn a_line_that_waited_goes_out_between_slices() {
    let (mut link, mut peer, _) = link();
    peer.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    link.send("console").unwrap();
    let mut buf = [0u8; 16];
    std::thread::sleep(LIVE);
    link.idle().unwrap();
    assert_eq!(peer.read(&mut buf).unwrap(), "console\n".len());
}

#[test]
fn tcp() {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap().to_string();
    let signal = Arc::new(AtomicBool::new(false));
    let mut link = SocketLink::tcp(&address, Some(Duration::from_secs(5)), signal).unwrap();
    let (mut peer, _) = listener.accept().unwrap();
    link.send("hello").unwrap();
    link.flush().unwrap();
    peer.write_all(b"wait\n").unwrap();
    assert_eq!(link.recv(None).unwrap(), "wait");
    let mut buf = [0u8; 6];
    peer.read_exact(&mut buf).unwrap();
    assert_eq!(&buf, b"hello\n");
    drop(listener);
    let signal = Arc::new(AtomicBool::new(false));
    assert!(SocketLink::tcp(&address, Some(Duration::from_secs(5)), signal).is_err());
}
