#!/usr/bin/env python
#
# Redirect data from a TCP/IP connection to a serial port and vice versa.
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
ST_DISCONNECTED = b"D"
ST_START = b"s"
ST_SERVER_HELLO_1 = b"\xe0"
ST_SERVER_HELLO_2 = b"\xe1"
ST_CLIENT_HELLO_1 = b"\xe2"
ST_CLIENT_HELLO_2 = b"\xe3"
ST_NUL = b"\x00" # sent first to be sure we are not in the escape state

class SerialToNet(serial.threaded.Protocol):
    """serial->socket"""

    def __init__(self, name: str, debug: bool) -> None:
        self.__name = name
        self.__debug = debug
        self.__socket: typing.Optional[socket.socket] = None
        self.__escape = False
        self.__started = False
        self.__control_message_queue: typing.Deque[ControlCommand] = collections.deque()
        self.__message_lock = threading.Condition()

    def __call__(self) -> serial.threaded.Protocol:
        return self

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
        if code == ST_DISCONNECTED:
            self.__started = False
            self.__socket = None

    def disconnect(self) -> None:
        self.__push_command(ST_DISCONNECTED)

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

def main() -> None:
    parser = argparse.ArgumentParser(
        description='Simple Serial to Network (TCP/IP) redirector.',
        epilog="""\
NOTE: no security measures are implemented. Anyone can remotely connect
to this service over the network.

Only one connection at once is supported. When the connection is terminated
it waits for the next connect.
""")

    parser.add_argument(
        'SERIALPORT',
        help="serial port name")

    parser.add_argument(
        'BAUDRATE',
        type=int,
        nargs='?',
        help='set baud rate, default: %(default)s',
        default=9600)

    parser.add_argument(
        '-q', '--quiet',
        action='store_true',
        help='suppress non error messages',
        default=False)

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

    group = parser.add_argument_group('serial port')

    group.add_argument(
        "--bytesize",
        choices=[5, 6, 7, 8],
        type=int,
        help="set bytesize, one of {5 6 7 8}, default: 8",
        default=8)

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
        '--xonxoff',
        action='store_true',
        help='enable software flow control (default off)',
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

    exclusive_group = group.add_mutually_exclusive_group()

    exclusive_group.add_argument(
        '-P', '--localport',
        type=int,
        help='local TCP port',
        default=7777)

    exclusive_group.add_argument(
        '-c', '--client',
        metavar='HOST:PORT',
        help='make the connection as a client, instead of running a server',
        default=False)

    args = parser.parse_args()

    # connect to serial port
    ser = serial.serial_for_url(args.SERIALPORT, do_not_open=True)
    ser.baudrate = args.BAUDRATE
    ser.bytesize = args.bytesize
    ser.parity = args.parity
    ser.stopbits = args.stopbits
    ser.rtscts = args.rtscts
    ser.xonxoff = args.xonxoff

    if args.rts is not None:
        ser.rts = args.rts

    if args.dtr is not None:
        ser.dtr = args.dtr

    if not args.quiet:
        sys.stderr.write(
            '--- TCP/IP to Serial redirect on {p.name}  {p.baudrate},{p.bytesize},{p.parity},{p.stopbits} ---\n'
            '--- type Ctrl-C / BREAK to quit\n'.format(p=ser))

    try:
        ser.open()
    except serial.SerialException as e:
        sys.stderr.write('Could not open serial port {}: {}\n'.format(ser.name, e))
        sys.exit(1)

    name = "Client" if args.client else "Server"
    ser_to_net = SerialToNet(name, args.debug)
    serial_worker = serial.threaded.ReaderThread(ser, ser_to_net)
    serial_worker.start()

    if not args.client:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(('', args.localport))
        srv.listen(1)
    try:
        intentional_exit = False
        while True:
            # The two sides of the serial connection should synchronise
            ready = False
            to_send = [ST_SERVER_HELLO_1, ST_SERVER_HELLO_2]
            to_receive = [ST_CLIENT_HELLO_1, ST_CLIENT_HELLO_2]
            if args.client:
                (to_send, to_receive) = (to_receive, to_send)

            sys.stderr.write('{}: waiting for serial link on {}...\n'.format(name, args.SERIALPORT))
            while not ready:
                for i in range(len(to_send)):
                    ser.write(ST_NUL + ST_ESCAPE + to_send[i])
                    timeout_at = time.monotonic() + 5.0
                    code = ser_to_net.get_control_message(1.0)
                    while ((code != to_receive[i])
                    and (code not in to_send)
                    and (time.monotonic() <= timeout_at)):
                        code = ser_to_net.get_control_message(1.0)

                    if code == to_receive[-1]:
                        # Appears to be ready
                        ready = True

                    if code in to_send:
                        sys.stderr.write('WARNING: Misconfiguration or echo detected. '
                            'One side should use -c, the other should use -P.\n')
                        break

                    if code is None:
                        # Try again - other side not ready yet
                        break

            # The connection is not yet set up. A connection to the server will begin it.
            if not args.client:
                sys.stderr.write('{}: serial link ok, waiting for connection on port {}...\n'.format(name, args.localport))
                client_socket, addr = srv.accept()
                (client_host, client_port) = addr
                sys.stderr.write('{}: connection from {}:{}\n'.format(name, client_host, client_port))
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

                ser_to_net.set_socket(client_socket)

                # Tell the other side of the serial cable to connect
                ser.write(ST_ESCAPE + ST_CONNECT)

                # Await message indicating connection or failure
                timeout_at = time.monotonic() + args.timeout
                code = ser_to_net.get_control_message(0.1)
                while ((code != ST_CONNECTED)
                and (code != ST_CONNECTION_FAILED)
                and (time.monotonic() <= timeout_at)):
                    code = ser_to_net.get_control_message(0.1)

                if code == ST_CONNECTION_FAILED:
                    sys.stderr.write('{}: ERROR: Connection failed on the client side\n'.format(name))
                    client_socket.close()
                    continue
                if code != ST_CONNECTED:
                    sys.stderr.write('{}: ERROR: Connection timeout on the client side\n'.format(name))
                    client_socket.close()
                    continue

            else:
                sys.stderr.write('{}: serial link ok, waiting for connection on port {}...\n'.format(name, args.SERIALPORT))

                # Await connect message from the other side of the serial cable
                while ser_to_net.get_control_message(1000.0) != ST_CONNECT:
                    pass

                client_host, client_port = args.client.split(':')
                sys.stderr.write("{}: Connecting to {}:{}...\n".format(name, client_host, client_port))
                client_socket = socket.socket()
                client_socket.settimeout(args.timeout)
                try:
                    client_socket.connect((client_host, int(client_port)))
                except socket.error as msg:
                    sys.stderr.write('{}: ERROR: Connection failed: {}\n'.format(name, msg))
                    # Tell the other side that the connection failed
                    ser.write(ST_ESCAPE + ST_CONNECTION_FAILED)
                    continue
                sys.stderr.write('{}: connected\n'.format(name))
                client_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

                ser_to_net.set_socket(client_socket)

                ser.write(ST_ESCAPE + ST_CONNECTED)

            # Start code indicates that the next bytes should be relayed
            ser.write(ST_ESCAPE + ST_START)

            try:
                # enter network <-> serial loop
                client_socket.settimeout(0.1)
                while True:
                    if ser_to_net.get_control_message() == ST_DISCONNECTED:
                        break # Disconnected

                    try:
                        data = client_socket.recv(4096)
                    except TimeoutError:
                        continue

                    if data == b"":
                        break # Disconnected

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

            except KeyboardInterrupt:
                intentional_exit = True
                raise
            except socket.error as msg:
                sys.stderr.write('ERROR: {}\n'.format(msg))
            except ConnectionResetError:
                pass
            finally:
                ser_to_net.disconnect()

                sys.stderr.write('{}: Disconnected\n'.format(name))
                client_socket.close()
                ser.write(ST_ESCAPE + ST_DISCONNECTED)

    except KeyboardInterrupt:
        pass

    sys.stderr.write('\n--- exit ---\n')
    serial_worker.stop()

if __name__ == '__main__':
    main()
