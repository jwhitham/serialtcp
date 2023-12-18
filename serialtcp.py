#!/usr/bin/env python
#
# This program redirects a TCP connection via a serial port.
#
# Based on https://github.com/pyserial/pyserial/blob/master/examples/tcp_serial_redirect.py
# but extended to make it work better for redirecting TCP services such as SSH.
#
# (C) 2002-2020 Chris Liechti <cliechti@gmx.net>
# (C) 2023 Jack Whitham <jack.d.whitham@gmail.com>
#
# SPDX-License-Identifier:    BSD-3-Clause

import sys
import socket
import serial  # type: ignore
import serial.threaded  # type: ignore
import time
import threading
import argparse
import typing
import collections

ControlCommand = bytes
ST_ESCAPE = b"\x1f"
ST_CONNECTED = b"c"
ST_CONNECT = b"C"
ST_CONNECTION_FAILED = b"F"
ST_DISCONNECT = b"D"
ST_START = b"s"
ST_SERVER_HELLO_0 = b"\xe0"
ST_SERVER_HELLO_1 = b"\xe1"
ST_SERVER_HELLO_2 = b"\xe2"
ST_CLIENT_HELLO_1 = b"\xc1"
ST_CLIENT_HELLO_2 = b"\xc2"
ST_NUL = b"\x00" # sent first to be sure we are not in the escape state

RETRY_TIMEOUT = 1.0

class SerialToNet:
    """Received serial data is separated into control messages and TCP data."""

    def __init__(self, name: str, debug: bool) -> None:
        self.__name = name
        self.__debug = debug
        self.__socket: typing.Optional[socket.socket] = None
        self.__escape = False
        self.__started = False
        self.__control_message_queue: typing.Deque[ControlCommand] = collections.deque()
        self.__message_lock = threading.Condition()
        self.__protocol = self.__SerialToNetProtocol(self)

    def get_protocol(self) -> serial.threaded.Protocol:
        return self.__protocol

    def get_control_message(self, timeout=0.0) -> typing.Optional[ControlCommand]:
        with self.__message_lock:
            # Get a message from the front of the queue, if any
            if len(self.__control_message_queue) != 0:
                return self.__control_message_queue.popleft()

            # Wait for the timeout and try again.
            if timeout > 0.0:
                self.__message_lock.wait(timeout)
                if len(self.__control_message_queue) != 0:
                    return self.__control_message_queue.popleft()

        return None

    def __push_command(self, code: ControlCommand) -> None:
        with self.__message_lock:
            self.__control_message_queue.append(code)
            self.__message_lock.notify_all()

        if code == ST_START and self.__socket is not None:
            self.__started = True
        if code == ST_DISCONNECT:
            self.__started = False
            self.__socket = None

    def disconnect(self) -> None:
        self.__push_command(ST_DISCONNECT)

    def set_socket(self, s: socket.socket) -> None:
        self.__socket = s
        self.__started = False

    def data_received(self, data: bytes) -> None:
        # Filter for commands and escape codes
        if self.__debug:
            sys.stderr.write('{}: receive data: {}\n'.format(self.__name, repr(data)))
        i = 0
        while i < len(data):
            code = data[i:i+1]
            if self.__escape:
                if code != ST_ESCAPE:
                    # A command of some kind - removed from the input
                    self.__push_command(code)

                    # Remove from input
                    data = data[:i] + data[i+1:]
                elif not self.__started:
                    # Junk before start - removed
                    data = data[:i] + data[i+1:]
                else:
                    # Literal ST_ESCAPE character - kept in the input
                    i += 1

                self.__escape = False
            else:
                if code == ST_ESCAPE:
                    # Escape code - removed from the input
                    self.__escape = True
                    data = data[:i] + data[i+1:]
                elif not self.__started:
                    # Junk before start - removed
                    data = data[:i] + data[i+1:]
                else:
                    # Literal character
                    i += 1

        # Forward remaining data to the socket
        try:
            if self.__socket is not None:
                self.__socket.sendall(data)
        except Exception:
            # In the event of an unexpected disconnection, try to stop
            self.disconnect()

    class __SerialToNetProtocol(serial.threaded.Protocol):
        """Wrapper for serial.threaded.Protocol created to allow full type-checking on SerialToNet."""
        def __init__(self, parent: "SerialToNet") -> None:
            self.__parent = parent

        def __call__(self) -> serial.threaded.Protocol:
            return self

        def data_received(self, data: bytes) -> None:
            self.__parent.data_received(data)


def do_synchronise(ser: serial.Serial, ser_to_net: SerialToNet,
                    name: str, debug: bool, is_client: bool, timeout: float) -> bool:
    """The client and server will use this to synchronise over the serial line,
    ensuring that both are present and in the same state."""

    # The message sequence for synchronisation
    to_send = [ST_SERVER_HELLO_1, ST_SERVER_HELLO_2]
    to_receive = [ST_CLIENT_HELLO_1, ST_CLIENT_HELLO_2]
    if is_client:
        (to_send, to_receive) = (to_receive, to_send)

    if debug:
        sys.stderr.write('{}: attempt to synchronise\n'.format(name))

    # Clear out received messages
    while ser_to_net.get_control_message() is not None:
        pass

    # Clear the escape flag on the other side
    ser.write(ST_NUL)

    # Send disconnect to force the other side to be ready to sync if it's connected
    ser.write(ST_ESCAPE + ST_DISCONNECT)

    # A server sends an initial optional message to tell a client it is ready;
    # this helps faster synchronisation if the client is already running.
    if not is_client:
        ser.write(ST_ESCAPE + ST_SERVER_HELLO_0)

    # Send each message
    for i in range(len(to_send)):
        # Client takes the initiative and sends a message first
        if is_client:
            if debug:
                sys.stderr.write('{}: send sync: {}\n'.format(name, repr(to_send[i])))
            ser.write(ST_ESCAPE + to_send[i])

        timeout_at = time.monotonic() + timeout
        code: typing.Optional[ControlCommand] = None
        while code != to_receive[i]:
            code = ser_to_net.get_control_message(max(0.0, timeout_at - time.monotonic()))

            if code in to_send:
                # Getting back what we are sending? Bad sign!
                sys.stderr.write('WARNING: Misconfiguration or echo detected. '
                    'One side should use --client, the other should use --server.\n')
                return False

            if (code == ST_SERVER_HELLO_0) and is_client:
                # Server just became ready. Immediate restart of sync.
                return False

            if (code in to_receive) and (code != to_receive[i]):
                # Restart sync if the wrong message was received
                return False

            if (code is None) and (time.monotonic() >= timeout_at):
                # Nothing received - no response from the other side
                # Give up this synchronisation attempt
                return False

        # Server responds to the client's message now
        if not is_client:
            if debug:
                sys.stderr.write('{}: send sync: {}\n'.format(name, repr(to_send[i])))
            ser.write(ST_ESCAPE + to_send[i])


    return True

def do_server_accept_connection(ser_to_net: SerialToNet, name: str,
                        server_socket: socket.socket) -> typing.Optional[socket.socket]:
    """The server will use this to accept a connection.

    If an ST_DISCONNECT message is received via the serial line, then
    the connection acceptance is aborted. This happens when the
    client side is stopped and restarted."""

    # Await connection
    while True:
        try:
            (client_socket, addr) = server_socket.accept()
            client_host = addr[0]
            client_port = addr[1]
            sys.stderr.write('{}: connection from {}:{}\n'.format(name, client_host, client_port))
            return client_socket
        except TimeoutError:
            # Check for disconnection command from the other side
            code = ser_to_net.get_control_message()
            while (code is not None) and (code != ST_DISCONNECT):
                code = ser_to_net.get_control_message()

            if code == ST_DISCONNECT:
                # Client went offline - return to sync state
                return None

def do_server_configure_socket(client_socket: socket.socket, timeout: float) -> None:
    """Configure the TCP socket on the server side."""

    # More quickly detect bad clients who quit without closing the
    # connection: After 1 second of idle, start sending TCP keep-alive
    # packets every 1 second. If 3 consecutive keep-alive packets
    # fail, assume the client is gone and close the connection.
    try:
        client_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 1)
        client_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 1)
        client_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
        client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except AttributeError:
        pass # XXX not available on windows
    client_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    client_socket.settimeout(timeout)

def do_server_await_client_connect(ser_to_net: SerialToNet, name: str,
            client_socket: socket.socket, timeout: float) -> bool:
    """Wait for the client to connect to the TCP port."""

    timeout_at = time.monotonic() + timeout
    code = None
    while ((code != ST_CONNECTED)
    and (code != ST_CONNECTION_FAILED)
    and (code != ST_DISCONNECT)
    and (time.monotonic() <= timeout_at)):
        code = ser_to_net.get_control_message(max(0.0, timeout_at - time.monotonic()))

    if code == ST_CONNECTION_FAILED:
        sys.stderr.write('{}: ERROR: Connection failed on the client side\n'.format(name))
        client_socket.close()
        return False
    if code == ST_DISCONNECT:
        sys.stderr.write('{}: ERROR: Connection aborted by the client side\n'.format(name))
        client_socket.close()
        return False
    if code != ST_CONNECTED:
        sys.stderr.write('{}: ERROR: Connection timeout on the client side\n'.format(name))
        client_socket.close()
        return False

    return True

def do_client_await_serial_connect(ser_to_net: SerialToNet, name: str) -> bool:
    """On the client side, await a connection message via the serial link."""
    code = None
    while (code != ST_CONNECT) and (code != ST_DISCONNECT):
        code = ser_to_net.get_control_message(1000.0)

    if code == ST_DISCONNECT:
        # Server went offline - return to sync state
        return False

    return True

def do_client_connect(client_host: str, client_port: int,
            timeout: float, name: str, address_family: int) -> typing.Optional[socket.socket]:
    """Connect to a TCP port."""
    sys.stderr.write("{}: connecting to {}:{}...\n".format(name, client_host, client_port))
    client_socket = socket.socket(address_family, socket.SOCK_STREAM)
    client_socket.settimeout(timeout)
    try:
        client_socket.connect((client_host, int(client_port)))
    except socket.error as msg:
        sys.stderr.write('{}: ERROR: Connection failed: {}\n'.format(name, msg))
        return None

    sys.stderr.write('{}: connected\n'.format(name))
    client_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return client_socket

def do_network_to_serial_loop(ser_to_net: SerialToNet, ser: serial.Serial,
                client_socket: socket.socket) -> bool:
    """Transfer data from network packets to serial."""
    if ser_to_net.get_control_message() == ST_DISCONNECT:
        return False # Disconnected

    try:
        data = client_socket.recv(4096)
    except TimeoutError:
        return True

    if data == b"":
        return False # Disconnected

    i = 0
    while i < len(data):
        if data[i:i+1] == ST_ESCAPE:
            # Escaping required for this character
            data = data[:i] + ST_ESCAPE + data[i:]
            i += 2
        else:
            # Literal character
            i += 1

    c = ser.write(data)
    if c != len(data):
        raise Exception(f"Unable to send {len(data)} bytes: sent {c}")

    return True


def do_main_loop(ser: serial.Serial, ser_to_net: SerialToNet, name: str,
            server_socket: typing.Optional[socket.socket], server_bind_address: str, server_port: int,
            client_host: str, client_port: int,
            debug: bool, timeout: float, serialport: str,
            address_family: int) -> None:

    # The two sides of the serial connection should synchronise
    sync_timeout = 1.0
    while not do_synchronise(ser=ser, ser_to_net=ser_to_net, name=name,
                        debug=debug, is_client=server_socket is None, timeout=sync_timeout):
        sync_timeout = min(60.0, sync_timeout * 1.5)

    # The connection is not yet set up. A connection to the server will begin it.
    if server_socket is not None:
        sys.stderr.write('{}: serial link ok, waiting for connection on {}:{}...\n'.format(
                name, server_bind_address, server_port))

        client_socket = do_server_accept_connection(
                    ser_to_net=ser_to_net, server_socket=server_socket, name=name)
        if client_socket is None:
            # Go back to synchronising
            return

        do_server_configure_socket(client_socket=client_socket, timeout=RETRY_TIMEOUT)
        ser_to_net.set_socket(client_socket)

        # Tell the other side of the serial cable to connect
        ser.write(ST_ESCAPE + ST_CONNECT)

        # Wait for the client to connect to the TCP port
        if not do_server_await_client_connect(ser_to_net=ser_to_net, name=name,
                        client_socket=client_socket, timeout=timeout):
            # Go back to synchronising
            return

    else:
        sys.stderr.write('{}: serial link ok, waiting for connection on {}...\n'.format(name, serialport))

        # Await connect message from the other side of the serial cable
        if not do_client_await_serial_connect(ser_to_net=ser_to_net, name=name):
            # Go back to synchronising
            return

        client_socket = do_client_connect(client_host=client_host, name=name,
                client_port=client_port, timeout=timeout,
                address_family=address_family)
        if client_socket is None:
            # Tell the other side that the connection failed
            ser.write(ST_ESCAPE + ST_CONNECTION_FAILED)
            # Go back to synchronising
            return

        ser_to_net.set_socket(client_socket)

        # Tell the other side of the serial cable that the connection is done
        ser.write(ST_ESCAPE + ST_CONNECTED)

    # Start code indicates that the next bytes should be relayed
    ser.write(ST_ESCAPE + ST_START)

    # enter network <-> serial loop
    try:
        client_socket.settimeout(RETRY_TIMEOUT)
        while do_network_to_serial_loop(ser_to_net=ser_to_net,
                    ser=ser, client_socket=client_socket):
            pass
    except socket.error as msg:
        sys.stderr.write('{}: ERROR: {}\n'.format(name, msg))
    except ConnectionResetError:
        pass
    finally:
        ser_to_net.disconnect()

        sys.stderr.write('{}: disconnected\n'.format(name))
        client_socket.close()
        ser.write(ST_ESCAPE + ST_DISCONNECT)

def main() -> None:
    parser = argparse.ArgumentParser(
        description="""
This program redirects a TCP connection via a serial port.

The program should run on both sides of a serial connection:

- On one side, it acts as a TCP server, and any connection to its TCP port
  will be forwarded across the serial cable.

- On the other side, it acts as a TCP client, and any connection via the
  serial cable will be forwarded to a specified IP address and port number.
""",
        epilog="""\
NOTE: no security measures are implemented. Anyone can remotely connect
to this service. By default the server port only binds to localhost interfaces.

Only one connection at once is supported. When the connection is terminated
it waits for the next connect.
""")

    parser.add_argument(
        'serialport',
        help="serial port name e.g. COM1, /dev/ttyS0")

    parser.add_argument(
        'baudrate',
        type=int,
        nargs='?',
        help='set baud rate, default: %(default)s',
        default=115200)

    parser.add_argument(
        '--debug',
        action='store_true',
        help='Development mode, prints data received',
        default=False)

    parser.add_argument(
        '--timeout',
        help='Connection timeout (seconds)',
        default=10.0)

    parser.add_argument(
        '--log',
        help='Log file name prefix')

    parser.add_argument(
        '-6', '--ipv6',
        action='store_true',
        help='Use IPv6')

    group = parser.add_argument_group('serial port')

    group.add_argument(
        "--parity",
        choices=['N', 'E', 'O', 'S', 'M'],
        type=lambda c: c.upper(),
        help="set parity, one of {N E O S M}, default: N",
        default='N')

    group.add_argument(
        "--stopbits",
        choices=[1, 1.5, 2],
        type=float,
        help="set stopbits, one of {1 1.5 2}, default: 1",
        default=1)

    group.add_argument(
        '--rtscts',
        action='store_true',
        help='enable RTS/CTS flow control (default off)',
        default=False)

    group.add_argument(
        '--rts',
        type=int,
        help='set initial RTS line state (possible values: 0, 1)',
        default=None)

    group.add_argument(
        '--dtr',
        type=int,
        help='set initial DTR line state (possible values: 0, 1)',
        default=None)

    group = parser.add_argument_group('network settings')

    exclusive_group = group.add_mutually_exclusive_group(required=True)

    exclusive_group.add_argument(
        '-s', '--server',
        metavar='[BIND_ADDRESS:]PORT',
        help='Act as a server using this local TCP port',
        default='')

    exclusive_group.add_argument(
        '-c', '--client',
        metavar='HOST:PORT',
        help='Act as a client and connect to this address',
        default='')

    args = parser.parse_args()
    assert args.client or args.server
    assert not (args.client and args.server)

    # connect to serial port
    ser = serial.serial_for_url(args.serialport, do_not_open=True)
    ser.baudrate = args.baudrate
    ser.bytesize = 8
    ser.parity = args.parity
    ser.stopbits = args.stopbits
    ser.rtscts = args.rtscts
    ser.xonxoff = False

    if args.rts is not None:
        ser.rts = args.rts

    if args.dtr is not None:
        ser.dtr = args.dtr

    name = "Client" if args.client else "Server"
    sys.stderr.write(f'{name}: serialtcp.py serial settings: {ser.name} {ser.baudrate},{ser.bytesize},{ser.parity},{ser.stopbits}\n')

    try:
        ser.open()
    except serial.SerialException as e:
        sys.stderr.write('Could not open serial port {}: {}\n'.format(ser.name, e))
        sys.exit(1)

    ser_to_net = SerialToNet(name, args.debug)
    serial_worker = serial.threaded.ReaderThread(ser, ser_to_net.get_protocol())
    serial_worker.start()
    client_host = server_bind_address = ""
    client_port = server_port = "0"
    server_socket: typing.Optional[socket.socket] = None
    address_family = socket.AF_INET6 if args.ipv6 else socket.AF_INET

    if args.server:
        server_bind_address, sep, server_port = args.server.rpartition(":")
        if sep == "":
            server_bind_address = "localhost"
            server_port = args.server
        server_socket = socket.socket(address_family, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.settimeout(RETRY_TIMEOUT)
        server_socket.bind((server_bind_address, int(server_port)))
        server_socket.listen(0)
        sys.stderr.write(f'{name}: serialtcp.py TCP service listening on {server_bind_address}:{server_port}\n')
    else:
        client_host, sep, client_port = args.client.rpartition(":")
        sys.stderr.write(f'{name}: serialtcp.py TCP client will connect to {client_host}:{client_port}\n')

    sys.stderr.write('{}: waiting for serial link on {}...\n'.format(name, args.serialport))

    try:
        while True:
            do_main_loop(ser=ser, ser_to_net=ser_to_net, name=name,
                        debug=args.debug, server_socket=server_socket,
                        server_bind_address=server_bind_address, server_port=int(server_port),
                        client_host=client_host, client_port=int(client_port),
                        timeout=args.timeout, serialport=args.serialport,
                        address_family=address_family)

    except KeyboardInterrupt:
        pass

    sys.stderr.write('{}: exit\n'.format(name))
    serial_worker.stop()

if __name__ == '__main__':
    main()
