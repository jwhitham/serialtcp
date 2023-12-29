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
import math
import enum
import logging

ControlCommand = bytes
ST_ESCAPE = b"\x1f"
ST_CONNECTED = b"c"
ST_CONNECT = b"C"
ST_CONNECTION_FAILED = b"F"
ST_DISCONNECT = b"D"
ST_START = b"s"
ST_SERVER_HELLO = b"\xe0"
ST_SERVER_SYNC_CODES = [b"\xe1", b"\xe2", b"\xe3"]
ST_CLIENT_SYNC_CODES = [b"\xc1", b"\xc2", b"\xc3"]

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
            logging.debug('%s: receive serial: %s', self.__name, repr(data))
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
                if self.__debug:
                    logging.debug('%s: forward data: %s', self.__name, repr(data))
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


class SynchroniseResult(enum.Enum):
    RETRY = enum.auto()
    OK = enum.auto()
    ECHO = enum.auto()

def do_synchronise(ser: serial.Serial, ser_to_net: SerialToNet,
                    name: str, debug: bool, is_client: bool, timeout: float,
                    ignore_echo: bool) -> SynchroniseResult:
    """The client and server will use this to synchronise over the serial line,
    ensuring that both are present and in the same state."""

    # The message sequence for synchronisation
    to_send = ST_SERVER_SYNC_CODES
    to_receive = ST_CLIENT_SYNC_CODES
    if is_client:
        (to_send, to_receive) = (to_receive, to_send)

    if debug:
        logging.debug('%s: attempt to synchronise', name)

    # Clear out received messages
    while ser_to_net.get_control_message() is not None:
        pass

    # Send disconnect to force the other side to be ready to sync if it's connected
    # The other side might be connected and in the escape state, in which case this will be
    # received as two literal characters. This is bad luck. The alternative is to
    # send some other character first, to clear the escape state, but then that will be
    # received as a literal character if it's NOT in the escape state - which is more likely..
    ser.write(ST_ESCAPE + ST_DISCONNECT + ST_ESCAPE + ST_DISCONNECT)

    # A server sends an initial optional message to tell a client it is ready;
    # this helps faster synchronisation if the client is already running.
    if not is_client:
        ser.write(ST_ESCAPE + ST_SERVER_HELLO)

    # Send each message
    for i in range(len(to_send)):
        # Client takes the initiative and sends a message first
        if is_client:
            if debug:
                logging.debug('%s: send sync: %s', name, repr(to_send[i]))
            ser.write(ST_ESCAPE + to_send[i])

        timeout_at = time.monotonic() + timeout
        code: typing.Optional[ControlCommand] = None
        while code != to_receive[i]:
            code = ser_to_net.get_control_message(max(0.0, timeout_at - time.monotonic()))

            if code in to_send:
                # Getting back what we are sending? Bad sign!
                if not ignore_echo:
                    logging.info('WARNING: Misconfiguration or echo detected. '
                        'One side should use --client, the other should use --server.\n')
                return SynchroniseResult.ECHO

            if (code == ST_SERVER_HELLO) and is_client:
                # Server just became ready. Immediate restart of sync.
                return SynchroniseResult.RETRY

            # Check for sync codes that are out of sequence
            # The correct sync code (i) is accepted
            # The previous sync code (i - 1) is ignored
            # Other sync codes are not accepted and restart the process
            if code is not None:
                try:
                    j = to_receive.index(code)
                    if (j != i) and (j != (i - 1)):
                        return SynchroniseResult.RETRY
                except ValueError:
                    pass

            if (code is None) and (time.monotonic() >= timeout_at):
                # Nothing received - no response from the other side
                # Give up this synchronisation attempt
                return SynchroniseResult.RETRY

        # Server responds to the client's message now
        if not is_client:
            if debug:
                logging.debug('%s: send sync: %s', name, repr(to_send[i]))
            ser.write(ST_ESCAPE + to_send[i])

    return SynchroniseResult.OK

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
            logging.info('%s: connection from %s:%s', name, client_host, client_port)
            return client_socket
        except (TimeoutError, socket.timeout):
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
        logging.error('%s: Connection failed on the client side', name)
        client_socket.shutdown(socket.SHUT_RDWR)
        client_socket.close()
        return False
    if code == ST_DISCONNECT:
        logging.error('%s: Connection aborted by the client side', name)
        client_socket.shutdown(socket.SHUT_RDWR)
        client_socket.close()
        return False
    if code != ST_CONNECTED:
        logging.error('%s: Connection timeout on the client side', name)
        client_socket.shutdown(socket.SHUT_RDWR)
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
    logging.info("%s: connecting to %s:%s...", name, client_host, client_port)
    client_socket = socket.socket(address_family, socket.SOCK_STREAM)
    client_socket.settimeout(timeout)
    try:
        client_socket.connect((client_host, int(client_port)))
    except socket.error as msg:
        logging.error('%s: ERROR: Connection failed: %s', name, msg)
        return None

    logging.info('%s: connected', name)
    client_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return client_socket

def do_network_to_serial_loop(ser_to_net: SerialToNet, ser: serial.Serial,
                client_socket: socket.socket, name: str, debug: bool) -> bool:
    """Transfer data from network packets to serial."""
    if ser_to_net.get_control_message() == ST_DISCONNECT:
        return False # Disconnected

    try:
        data = client_socket.recv(4096)
    except (TimeoutError, socket.timeout):
        if debug:
            logging.debug('%s: receive network timeout', name)
        return True

    if data == b"":
        if debug:
            logging.debug('%s: receive network end', name)
        return False # Disconnected

    if debug:
        logging.debug('%s: receive network: %s', name, repr(data))

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
            address_family: int, sync_timeout: float) -> None:

    # The two sides of the serial connection should synchronise
    attempt_timeout = 1.0
    timeout_at = time.monotonic() + sync_timeout
    result = SynchroniseResult.RETRY
    ignore_echo = False
    while result != SynchroniseResult.OK:
        result = do_synchronise(ser=ser, ser_to_net=ser_to_net, name=name,
                        debug=debug, is_client=server_socket is None,
                        ignore_echo=ignore_echo,
                        timeout=max(0.0,
                            min(timeout_at - time.monotonic(), attempt_timeout)))
        if result == SynchroniseResult.ECHO:
            ignore_echo = True
        if (result != SynchroniseResult.OK) and (time.monotonic() > timeout_at):
            # No more attempts
            logging.error('%s: unable to connect to remote via %s', name, serialport)
            sys.exit(2)
        attempt_timeout = min(60.0, attempt_timeout * 1.5)

    # The connection is not yet set up. A connection to the server will begin it.
    if server_socket is not None:
        logging.info('%s: serial link ok, waiting for connection on %s:%s...',
                name, server_bind_address, server_port)

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
        logging.info('%s: serial link ok, waiting for connection on %s...', name, serialport)

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
        while do_network_to_serial_loop(ser_to_net=ser_to_net, debug=debug,
                    ser=ser, client_socket=client_socket, name=name):
            pass
    except socket.error as msg:
        logging.error('%s: %s', name, msg)
    except ConnectionResetError:
        pass
    finally:
        ser_to_net.disconnect()

        logging.info('%s: disconnected', name)
        client_socket.shutdown(socket.SHUT_RDWR)
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
        type=float,
        default=10.0)

    parser.add_argument(
        '-6', '--ipv6',
        action='store_true',
        help='Use IPv6')

    parser.add_argument(
        '--log',
        help='Log file')

    # This parameter is just for testing, and creates a file containing the server port number
    parser.add_argument(
        '--test-port-file',
        help=argparse.SUPPRESS)

    # Synchronisation timeout. Exit with an error if unable to synchronise
    # within this time.
    parser.add_argument(
        '--sync-timeout',
        type=float,
        default=math.inf,
        help='Timeout for synchronisation via the serial line (seconds)')

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
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, force=True,
                         format='%(asctime)s %(message)s',
                         filename=args.log)

    # connect to serial port
    ser = serial.serial_for_url(args.serialport, do_not_open=True, exclusive=True)
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
    logging.info(f'{name}: serialtcp.py serial settings: {ser.name} {ser.baudrate},{ser.bytesize},{ser.parity},{ser.stopbits}\n')

    try:
        ser.open()
    except serial.SerialException as e:
        logging.error('Could not open serial port %s: %s\n', ser.name, e)
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
        server_port = str(server_socket.getsockname()[1])
        server_socket.listen(0)
        logging.info('%s: serialtcp.py TCP service listening on %s:%s',
                    name, server_bind_address, server_port)
        if args.test_port_file:
            with open(args.test_port_file, "wt") as fd:
                fd.write(f"{server_port}\n")
    else:
        client_host, sep, client_port = args.client.rpartition(":")
        logging.info('%s: serialtcp.py TCP client will connect to %s:%s',
                name, client_host, client_port)

    logging.info('%s: waiting for serial link on %s...', name, args.serialport)

    try:
        while True:
            do_main_loop(ser=ser, ser_to_net=ser_to_net, name=name,
                        debug=args.debug, server_socket=server_socket,
                        server_bind_address=server_bind_address, server_port=int(server_port),
                        client_host=client_host, client_port=int(client_port),
                        timeout=args.timeout, serialport=args.serialport,
                        address_family=address_family, sync_timeout=args.sync_timeout)

    except KeyboardInterrupt:
        pass

    logging.info('%s: exit', name)
    serial_worker.stop()

if __name__ == '__main__':
    main()
