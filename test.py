#!/usr/bin/env python
#
# Test for serialtcp
#
# You need to set up two serial ports on the same PC, and connect them together with
# a null modem cable. I'm using two USB serial adapters, both with TTL outputs.
#

import pytest
import socket, threading, socketserver, tempfile
import subprocess, time, random, struct, sys, typing
import serial.tools.list_ports  # type: ignore
from pathlib import Path

LOCAL_ADDRESS = "localhost"
PROGRAM = [sys.executable, str(Path(__file__).parent / "serialtcp.py")]
DEVICES = [__d.device for __d in serial.tools.list_ports.comports()
            if __d.name not in ("COM1", "ttyS0")]
assert len(DEVICES) >= 2, "There must be at least two serial port devices attached (excluding COM1)"
DEVICE_0 = DEVICES[0]
DEVICE_1 = DEVICES[1]
SPEED = 115200
TEST_TIMEOUT = 10.0
DEBUG = False



@pytest.fixture
def loopback_server() -> typing.Iterator[typing.Tuple[int, typing.Callable[[], None]]]:
    running = [True]

    class ThreadedTCPLoopback(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            while running[0]:
                self.request.settimeout(0.1)
                try:
                    data = self.request.recv(1024)
                except (TimeoutError, socket.timeout):
                    continue # No data received yet
                except (ConnectionAbortedError, ConnectionResetError):
                    data = b"" # Seen on Windows only
                if data == b"":
                    return # Disconnected
                self.request.settimeout(None)
                self.request.sendall(data)

    class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        pass

    def stop() -> None:
        if running[0]:
            running[0] = False
            server.shutdown()

    server = ThreadedTCPServer((LOCAL_ADDRESS, 0), ThreadedTCPLoopback)
    with server:
        (_, port) = server.server_address
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.daemon = True
        server_thread.start()
        yield (port, stop)
        stop()

@pytest.fixture
def loopback_port(loopback_server) -> typing.Iterator[int]:
    (port, _) = loopback_server
    yield port

def connect_to_server(port: int) -> socket.socket:
    end_time = time.monotonic() + TEST_TIMEOUT
    s: typing.Optional[socket.socket] = None
    while (time.monotonic() < end_time) and not s:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1.0)
            s.connect((LOCAL_ADDRESS, port))
        except socket.error:
            # Try again
            time.sleep(0.5)
            s = None

    assert s is not None, f"Server was not reachable on port {port} within {TEST_TIMEOUT} seconds"
    s.settimeout(None)
    return s

def start_server(args: typing.List[str]) -> typing.Tuple[subprocess.Popen, int]:
    with tempfile.TemporaryDirectory() as td:
        tpf = Path(td) / "test-port-file"
        server = subprocess.Popen(PROGRAM +
                ["-s", "0", "--test-port-file", str(tpf),
                    DEVICE_1, str(SPEED)] + args,
                stdin=subprocess.DEVNULL)

        end_time = time.monotonic() + TEST_TIMEOUT
        port = 0
        while (time.monotonic() < end_time) and (port == 0):
            time.sleep(0.1)
            try:
                with open(tpf, "rt") as fd:
                    port_text = fd.read()
                if port_text.endswith("\n"):
                    port = int(port_text.strip())
            except Exception:
                pass
        assert port != 0, f"Server did not start up within {TEST_TIMEOUT} seconds"

    return (server, port)

def start_client(port: int, args: typing.List[str]) -> subprocess.Popen:
    return subprocess.Popen(PROGRAM +
            ["-c", f"{LOCAL_ADDRESS}:{port}", DEVICE_0, str(SPEED)] + args,
            stdin=subprocess.DEVNULL)

@pytest.fixture
def server_socket() -> typing.Iterator[socket.socket]:
    (server, port) = start_server(["--debug"] if DEBUG else [])
    s = connect_to_server(port)
    yield s
    server.kill()

@pytest.fixture
def loopback_client(loopback_port: int) -> typing.Iterator[int]:
    client = start_client(loopback_port, ["--debug"] if DEBUG else [])
    yield 0
    client.kill()

def receive_and_transmit(server_socket, size) -> None:
    r = random.Random(size)
    data: typing.List[bytes] = []
    if DEBUG:
        while len(data) < size:
            data.append(struct.pack("<B", size & 0xff))
    else:
        while (len(data) * 8) < size:
            data.append(struct.pack("Q", r.randrange(0, 1 << 64)))

    to_send = b"".join(data)[:size]
    bytes_sent = 0
    bytes_received = 0
    received: typing.List[bytes] = []
    end_time = time.monotonic() + TEST_TIMEOUT

    while (bytes_received < size) and (time.monotonic() < end_time):
        if bytes_sent < size:
            sent_count = server_socket.send(to_send[bytes_sent:size])
            assert sent_count >= 0
            bytes_sent += sent_count

        incoming = server_socket.recv(min(1 << 16, size - bytes_received))
        if len(incoming) == 0:
            time.sleep(0.1)
        else:
            received.append(incoming)
            bytes_received += len(incoming)

    got_back = b"".join(received)
    if to_send.startswith(got_back):
        print(f"to_send starts with got_back - {len(got_back)} checked")
    else:
        print("to_send does not start with got_back")
        print("to_send =", repr(to_send[:20]))
        print("got_back =", repr(got_back[:20]))

    assert bytes_sent == size, f"Did not send {size} bytes within {TEST_TIMEOUT} seconds, only {bytes_sent}"
    assert bytes_received == size, f"Did not receive {size} bytes within {TEST_TIMEOUT} seconds, only {bytes_received}"
    assert got_back == to_send, "Loopback data was corrupt"

def test_small_loopback(server_socket, loopback_client) -> None:
    """Send a series of small messages"""
    for i in range(10):
        receive_and_transmit(server_socket, i + 1)

def test_mid_loopback(server_socket, loopback_client) -> None:
    """Send a series of mid-size messages"""
    for i in range(10):
        receive_and_transmit(server_socket, (i + 1) * 250)

def test_big_loopback1(server_socket, loopback_client) -> None:
    """Send larger message 1"""
    receive_and_transmit(server_socket, 66666)

def test_big_loopback2(server_socket, loopback_client) -> None:
    """Send larger message 2"""
    receive_and_transmit(server_socket, 33333)

def test_server_reconnect(server_socket, loopback_client) -> None:
    """Disconnect from the server during the test, then reconnect.
    Checks that the link can be re-established."""
    receive_and_transmit(server_socket, 19)
    port = server_socket.getpeername()[1]
    for i in range(5):
        server_socket.close()
        server_socket = connect_to_server(port)
        receive_and_transmit(server_socket, 20 + i)

def test_server_restart(loopback_client) -> None:
    """Stop the server during the test, then restart it, without restarting the client.
    Checks that the link can be re-established."""
    for i in range(5):
        (server, port) = start_server([])
        try:
            try:
                server_socket = connect_to_server(port)
                receive_and_transmit(server_socket, 11 + i)
            finally:
                server_socket.close()
        finally:
            server.kill()

def test_client_restart(loopback_port) -> None:
    """Stop the client during the test, then restart it.
    Link has to be re-established by reconnecting to the server."""

    (server, port) = start_server([])
    try:
        try:
            server_socket = connect_to_server(port)
            server_socket.settimeout(0.5)

            # This part is sent ok
            try:
                client = start_client(loopback_port, [])
                server_socket.sendall(b"11111")
                received = b""
                for i in range(5):
                    received += server_socket.recv(1)
                assert received == b"11111"
            finally:
                client.kill()

            # None of the following message is received.
            # The server sends the message on the serial line, but has no way of knowing
            # that the client has gone. There is no response.
            server_socket.send(b"22222")
            with pytest.raises(TimeoutError):
                try:
                    server_socket.recv(1)
                except socket.timeout:
                    raise TimeoutError from None # Different exception on Windows

            # Now we can restart the client; this will cause a resync. The server socket
            # will be disconnected (FIN and RST messages).
            try:
                client = start_client(loopback_port, [])
                time.sleep(1.0)
                # This ought to result in Connection Reset By Peer, but we don't see that
                # until the second message is sent
                with pytest.raises(BrokenPipeError):
                    try:
                        server_socket.send(b"3")
                        server_socket.send(b"3")
                    except (ConnectionAbortedError, ConnectionResetError):
                        raise BrokenPipeError from None # Different exception on Windows

                # Reconnect to the server in order to carry on
                server_socket = connect_to_server(port)

                server_socket.sendall(b"44444")
                received = b""
                for i in range(5):
                    received += server_socket.recv(1)
                assert received == b"44444"
            finally:
                client.kill()
        finally:
            server_socket.close()
    finally:
        server.kill()

# Test: server can't reach the client
def test_server_cant_reach_client() -> None:
    """Client is not running. Server cannot synchronise."""
    server = subprocess.Popen(PROGRAM +
            ["-s", "0", "--sync-timeout", "2.0", DEVICE_1, str(SPEED)],
            stdin=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True)
    (_, stderr) = server.communicate()
    rc = server.wait()
    assert "unable to connect to remote" in stderr
    assert rc != 0

# Test: client can't reach the server
def test_client_cant_reach_server() -> None:
    """Server is not running. Client cannot synchronise."""
    client = subprocess.Popen(PROGRAM +
            ["-c", f"{LOCAL_ADDRESS}:1",
            "--sync-timeout", "2.0", DEVICE_0, str(SPEED)],
            stdin=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True)
    (_, stderr) = client.communicate()
    rc = client.wait()
    assert "unable to connect to remote" in stderr
    assert rc != 0

# Test: server and client using different speeds
def test_server_client_different_speed(server_socket) -> None:
    """Client and server use different speeds and can't communicate."""
    client = subprocess.Popen(PROGRAM +
            ["-c", f"{LOCAL_ADDRESS}:1",
            "--sync-timeout", "2.0", DEVICE_0, "9600"],
            stdin=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True)
    (_, stderr) = client.communicate()
    rc = client.wait()
    assert "unable to connect to remote" in stderr
    assert rc != 0

# Test: client can't connect to the specified address:port
def test_client_cant_connect(server_socket) -> None:
    """Client's target address is not valid - can't connect. Error
    should be reported when connecting to the server."""
    try:
        client = start_client(1, [])
        server_socket.send(b"55555")
        try:
            received = server_socket.recv(1)
        except (ConnectionAbortedError, ConnectionResetError):
            # We get this error on Windows but on Linux the recv function just returns b"".
            # Either is ok.
            received = b""
        assert received == b""  # Connection failed on the client side
    finally:
        client.kill()

# Test: loopback service shuts down while in progress
def test_kill_loopback(server_socket, loopback_server, loopback_client) -> None:
    """Client's target is killed - check for correct behaviour."""
    (_, stop) = loopback_server
    receive_and_transmit(server_socket, 5)
    stop()
    time.sleep(0.5)
    server_socket.send(b"66666")
    try:
        received = server_socket.recv(1)
    except (ConnectionAbortedError, ConnectionResetError):
        # Exception seen on Windows
        received = b""
    assert received == b""  # No more connection until we reconnect to the server

if __name__ == "__main__":
    pytest.main(sys.argv[1:] if len(sys.argv) > 1 else [__file__])
