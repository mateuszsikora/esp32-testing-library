"""The runs the determinism job runs twice and compares (plan §6): `make determinism` runs this
file twice, each time under a `--basetemp` of its own, and `scripts/check-determinism.py` then
finds the two runs identical or prints what differs (docs/guides/testing.md#determinism).

Each test leaves in its `tmp_path` what the job compares: `console.txt`, every line nmea_reader
printed with the board's time it ended at, and `uart.gnss.jsonl`, the tap of the GNSS route. Each
test also ends the run at a time of its own choosing, so that two runs end at the same instant.
Alone, in the suite, each checks that its run did what it should, so that two runs that both did
nothing cannot pass the job.
"""

import json
from pathlib import Path

import pytest
from test_bt_spp_initiator import NAME, PIN, Heard
from test_net_tls import tls_exchange

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.bluetooth.peer import SppPeer
from esp32tl.flash import u16
from esp32tl.interfaces.bluetooth import Bluetooth
from esp32tl.interfaces.console import Console
from esp32tl.interfaces.gnss import Gnss
from esp32tl.interfaces.modem import Modem
from esp32tl.interfaces.sd_card import SdCard
from esp32tl.interfaces.wifi import Wifi
from esp32tl.net import Internet
from esp32tl.net.mqtt import Broker
from esp32tl.net.tls import CertificateAuthority
from esp32tl.report import board_time
from esp32tl.units import EAST, NORTH, Position, kbit_s, km, km_h, ms, seconds

pytestmark = pytest.mark.app_path("../firmware/nmea_reader")

WARSAW = Position(52.229677, 21.012233)

# A fix line as nmea_reader prints it.
FIX = r"^fix(?: acquired)? lat=\S+ lon=\S+$"


# gnss-neo-m8n, a real receiver's log converted, has its first fix at 2 s. The ids are short, so
# that each case's tmp_path differs in the first 30 characters of its name, all pytest keeps: two
# cases named alike would be numbered by the order they ran in, which xdist changes (#287).
@pytest.mark.parametrize(
    "recording",
    ["gnss-generated-1", "gnss-generated-2", "gnss-neo-m8n"],
    ids=["gen-1", "gen-2", "neo-m8n"],
)
def test_a_replay(
    recording: str, board: Esp32Sim, gnss: Gnss, console: Console, tmp_path: Path
) -> None:
    gnss.replay(f"../recordings/{recording}.jsonl")
    gnss.tap(tmp_path / "uart.gnss.jsonl")
    board.power_on()
    console.expect_exact("fix acquired")
    board.wait(seconds(20))
    write_console(tmp_path, console)
    # One fix a second from the first, and the run ends 20 s past it.
    assert len(console.lines(FIX)) == 21


def test_the_receiver_model(board: Esp32Sim, gnss: Gnss, console: Console, tmp_path: Path) -> None:
    gnss.fix(at=WARSAW)
    gnss.tap(tmp_path / "uart.gnss.jsonl")
    board.power_on()
    board.wait_until(console.line(FIX), within=seconds(5))
    gnss.move(heading=NORTH, speed=km_h(36))
    board.wait(seconds(10))
    gnss.lose_fix()
    board.wait(seconds(5))
    gnss.fix(at=WARSAW.moved(EAST, km(2)))
    board.wait(seconds(5))
    write_console(tmp_path, console)
    assert len(console.lines(FIX)) >= 15


def write_console(directory: Path, console: Console) -> None:
    """Writes `console.txt` into `directory`: each line the firmware has printed, after the
    board's time it ended at."""
    lines = (f"{board_time(line.at.ns)}  {line.text}\n" for line in console.lines())
    (directory / "console.txt").write_text("".join(lines))


@pytest.mark.app_path("../firmware/pppos_client")
@pytest.mark.board_profile("lilygo-t-a7670g-r2")
def test_plain_mqtt(board: Esp32Sim, modem: Modem, broker: Broker, tmp_path: Path) -> None:
    modem.tap(tmp_path / "uart.modem.jsonl")
    board.power_on()
    message = board.wait_until(broker.next_message("/topic/esp-pppos"), within=seconds(60))
    assert message.text == "esp32-pppos"
    (tmp_path / "network.json").write_text(json.dumps(modem.network().transcript()))


def write_link_changes(directory: Path, network: Internet) -> None:
    """Writes `link_changes.txt` into `directory`: each time the link came up or went down, after
    the board's time it did, and why."""
    lines = (
        f"{board_time(c.at.ns)}  {'up' if c.up else 'down'}  {c.cause}\n"
        for c in network.link_changes
    )
    (directory / "link_changes.txt").write_text("".join(lines))


@pytest.mark.app_path("../firmware/modem_recovery")
@pytest.mark.board_profile("lilygo-t-a7670g-r2")
def test_a_dropped_call_and_the_redial(
    board: Esp32Sim, modem: Modem, console: Console, tmp_path: Path
) -> None:
    network = modem.network()
    board.power_on()
    board.wait_until(console.line("modem_recovery: connected"), within=seconds(60))
    down, up = network.next_link_down(), network.next_link_up()
    modem.drop_call()
    assert down() is not None
    board.wait_until(up, within=seconds(90))
    board.wait(seconds(2))
    write_console(tmp_path, console)
    write_link_changes(tmp_path, network)
    assert [(c.up, c.cause) for c in network.link_changes] == [
        (True, "IPCP open"),
        (False, "call ended"),
        (True, "IPCP open"),
    ]


@pytest.mark.app_path("../firmware/mqtt_session")
@pytest.mark.board_profile("lilygo-t-a7670g-r2")
def test_mqtt_session_on_a_path(
    board: Esp32Sim, modem: Modem, broker: Broker, console: Console, tmp_path: Path
) -> None:
    network = modem.network()
    network.set_path(rtt=ms(200), downlink=kbit_s(512), uplink=kbit_s(128))
    modem.tap(tmp_path / "uart.modem.jsonl")
    board.power_on()
    board.wait_until(broker.is_subscribed("session/commands"), within=seconds(60))
    done = console.line(r"mqtt_session: backlog done")
    broker.publish("session/commands", "backlog 5", qos=1)
    board.wait_until(done, within=seconds(30))
    reconnected = broker.next_connection()
    broker.publish("session/commands", "in flight", qos=1)
    assert broker.disconnect("session-client")
    assert board.wait_until(reconnected, within=seconds(30)).session_present
    board.wait(seconds(2))
    write_console(tmp_path, console)
    assert console.lines(r"data dup=1 qos=1 .* payload=in flight$")
    assert sum(m.topic == "session/reports" and not m.dup for m in broker.messages) == 5
    (tmp_path / "network.json").write_text(json.dumps(network.transcript()))


def test_tls_plaintext(tmp_path: Path) -> None:
    (tmp_path / "network.json").write_text(json.dumps(tls_exchange()))


@pytest.mark.app_path("../examples/position_tracker")
@pytest.mark.board_profile("lilygo-t-a7670g-r2")
def test_tls_on_the_device(
    board: Esp32Sim, modem: Modem, broker: Broker, console: Console, tmp_path: Path
) -> None:
    # The firmware verifies the CA, does its ECDHE and publishes over TLS, through PPP: the times
    # after the handshake repeat only if the server's bytes do.
    network = modem.network()
    ca = CertificateAuthority(["broker.test"])
    network.listen(8883, broker, tls=ca)
    board.nvs(
        {
            "config": {
                "broker_host": "broker.test",
                "broker_port": u16(8883),
                "broker_ca": ca.ca_pem,
                "ntp_host": "time.test",
                "apn": "internet",
            }
        }
    )
    modem.tap(tmp_path / "uart.modem.jsonl")
    board.power_on()
    board.wait_until(broker.next_message("tracker/position"), within=seconds(60))
    board.wait(seconds(25))
    write_console(tmp_path, console)
    assert sum(m.topic == "tracker/position" for m in broker.messages) == 3
    (tmp_path / "network.json").write_text(json.dumps(network.transcript()))


@pytest.mark.app_path("../firmware/http_request")
def test_wifi_http(board: Esp32Sim, wifi: Wifi, console: Console, tmp_path: Path) -> None:
    network = wifi.access_point("myssid", password="mypassword")
    network.http.serve("/", "hello from the test\n")
    board.power_on()
    board.wait_until(network.http.next_request("/"), within=seconds(30))
    board.wait(seconds(2))
    write_console(tmp_path, console)
    assert console.lines(r"^hello from the test$")
    (tmp_path / "network.json").write_text(json.dumps(network.transcript()))


@pytest.mark.app_path("../firmware/bt_spp_initiator")
def test_spp_to_a_peer(board: Esp32Sim, bt: Bluetooth, console: Console, tmp_path: Path) -> None:
    source = Heard()
    peer = SppPeer(bt.air, name=NAME, pin=PIN, source=source)
    bt.tap(tmp_path / "hci.btsnoop")
    board.power_on()
    board.wait(seconds(5))
    write_console(tmp_path, console)
    assert console.lines(r"speed\(") and peer.channel_open
    # Each write the peer heard: its time and its bytes, which are the firmware's blocks.
    writes = (f"{board_time(at.ns)}  {data.hex()}\n" for at, data in source.heard)
    (tmp_path / "spp.txt").write_text("".join(writes))


@pytest.mark.app_path("../firmware/bt_spp_initiator")
def test_spp_with_simple_pairing(
    board: Esp32Sim, bt: Bluetooth, console: Console, tmp_path: Path
) -> None:
    peer = SppPeer(bt.air, name=NAME, pin=PIN, source=Heard(), io_capability="no_input_no_output")
    bt.tap(tmp_path / "hci.btsnoop")
    board.power_on()
    board.wait(seconds(3))
    write_console(tmp_path, console)
    assert peer.channel_open and [p.method for p in peer.pairings] == ["just_works"]
    # Each pairing on both sides: its time, how it went, the number both showed.
    pairings = [(p.t.ns, p.initiator, p.method, p.number, int(p.status)) for p in peer.pairings]
    pairings += [(p.t.ns, p.initiator, p.method, p.number, int(p.status)) for p in bt.pairings()]
    (tmp_path / "pairings.json").write_text(json.dumps(pairings))


@pytest.mark.app_path("../firmware/sd_remount")
@pytest.mark.requires_mtools
def test_sd_card_faults(board: Esp32Sim, sd_card: SdCard, console: Console, tmp_path: Path) -> None:
    card = sd_card.image(size_mb=32).with_text("README.TXT", "the card\n").build()
    sd_card.insert(card)
    board.power_on()
    board.wait_until(console.line(r"^sd_remount: wrote 3$"), within=seconds(10))
    # One write fails: the firmware unmounts the card and mounts it again (#287).
    failed = sd_card.fail("write", count=1)
    board.wait_until(console.line(r"^sd_remount: mounted 2$"), within=seconds(10))
    # Four writes keep the card busy, then the card goes out and comes back.
    slowed = sd_card.delay("write", ms(250), count=4)
    board.wait_until(lambda: len(slowed.hits) == 4, within=seconds(10))
    sd_card.remove()
    board.wait_until(console.line(r"^sd_remount: no card"), within=seconds(10))
    sd_card.insert(card)
    board.wait_until(console.line(r"^sd_remount: mounted 3$"), within=seconds(10))
    board.wait(seconds(1))
    write_console(tmp_path, console)
    assert len(failed.hits) == 1 and not failed.hits[0].written
    assert len(console.lines(r"^sd_remount: write failed \(I/O error\)$")) == 2
    # Each operation a rule met and each change of card, at its board time; and what the card
    # holds at the end.
    faulted = (f"{board_time(hit.at.ns)}  {hit.fault.id}  {hit}\n" for hit in sd_card.faulted)
    changes = (f"{board_time(c.t)}  {c.image is not None}\n" for c in sd_card.changes)
    (tmp_path / "sd_card.txt").write_text("".join([*faulted, *changes]))
    (tmp_path / "LOG.TXT").write_text(card.read_text("LOG.TXT"))


@pytest.mark.app_path("../firmware/arduino_shell", build_dir=".pio/build/esp32dev")
@pytest.mark.parametrize("password", [None, "provisioning"], ids=["open", "wpa2"])
def test_device_softap(
    board: Esp32Sim, wifi: Wifi, console: Console, tmp_path: Path, password: str | None
) -> None:
    board.nvs({"shell": {"ap_ssid": "device-setup", "ap_password": password or ""}})
    network = wifi.station("device-setup", password=password)
    board.power_on()
    lease = board.wait_until(network.is_leased, within=seconds(3))
    exchange = network.http_client.get("/status")
    response = board.wait_until(exchange.response, within=seconds(2))
    assert response.status == 200 and response.json()["status"] == "ok"
    console.expect_exact("arduino_shell: GET /status -> 200")
    write_console(tmp_path, console)
    (tmp_path / "network.json").write_text(json.dumps(network.transcript(), sort_keys=True))
    (tmp_path / "lease.txt").write_text(str(lease))


@pytest.mark.app_path("../firmware/arduino_shell", build_dir=".pio/build/esp32dev")
def test_device_softap_left_and_joined_again(
    board: Esp32Sim, wifi: Wifi, console: Console, tmp_path: Path
) -> None:
    board.nvs({"shell": {"ap_ssid": "device-setup"}})
    network = wifi.station("device-setup")
    board.power_on()
    board.wait_until(network.is_leased, within=seconds(3))
    left = network.next_link_down()
    console.sendline("ap-off")
    board.wait_until(left, within=seconds(5))
    joined = network.next_link_up()
    console.sendline("ap-on")
    board.wait_until(joined, within=seconds(5))
    board.wait_until(network.is_leased, within=seconds(5))
    board.wait(seconds(1))
    write_console(tmp_path, console)
    write_link_changes(tmp_path, network)
    assert [(c.up, c.cause) for c in network.link_changes] == [
        (True, "station joined"),
        (False, "station left"),
        (True, "station joined"),
    ]
