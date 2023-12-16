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
import serial
import serial.threaded
import time
import threading

ST_ESCAPE = b"\x1f"
ST_CONNECTED = b"c"
ST_CONNECT = b"C"
ST_CONNECTION_FAILED = b"F"
ST_DISCONNECTED = b"D"
ST_START = b"s"
ST_NUL = b"\x00" # sent first to be sure we are not in the escape state

class SerialToNet(serial.threaded.Protocol):
    """serial->socket"""

    def __init__(self):
        self.socket = None
        self.escape = False
        self.started = False
        self.control_messages = set()
        self.message_lock = threading.Condition()
        self.socket_lock = threading.Condition()

    def __call__(self):
        return self

    def data_received(self, data):
        i = 0
        while i < len(data):
            code = data[i:i+1]
            if self.escape:
                if code != ST_ESCAPE:
                    # A command of some kind - removed from the input
                    with self.message_lock:
                        self.control_messages.add(code)
                        self.message_lock.notify_all()
                    if code == ST_START:
                        self.started = True
                    if code == ST_DISCONNECTED:
                        self.started = False
                    data = data[:i] + data[i+1:]
                elif not self.started:
                    # Junk before start - removed
                    data = data[:i] + data[i+1:]
                else:
                    # Literal ST_ESCAPE character - kept in the input
                    i += 1
                self.escape = False
            else:
                if code == ST_ESCAPE:
                    # Escape code - removed from the input
                    self.escape = True
                    data = data[:i] + data[i+1:]
                elif not self.started:
                    # Junk before start - removed
                    data = data[:i] + data[i+1:]
                else:
                    # Literal character
                    i += 1

        try:
            with self.socket_lock:
                if self.socket is not None:
                    self.socket.sendall(data)
        except Exception:
            # In the event of an unexpected disconnection, try to stop
            with self.socket_lock:
                self.socket = None
            with self.message_lock:
                self.control_messages.add(ST_DISCONNECTED)
                self.message_lock.notify_all()
            self.started = False

if __name__ == '__main__':  # noqa
    import argparse

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
        '--develop',
        action='store_true',
        help='Development mode, prints Python internals on errors',
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

    ser_to_net = SerialToNet()
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
            # The connection is not yet set up. A connection to the server will begin it.
            if not args.client:
                sys.stderr.write('Waiting for connection on {}...\n'.format(args.localport))
                client_socket, addr = srv.accept()
                sys.stderr.write('Connected by {}\n'.format(addr))
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

                with ser_to_net.socket_lock:
                    ser_to_net.socket = client_socket

                # Tell the other side of the serial cable to connect
                ser.write(ST_NUL + ST_ESCAPE + ST_CONNECT)

                # Await message indicating connection or failure
                with ser_to_net.message_lock:
                    timeout_at = time.monotonic() + args.timeout
                    messages_copy = set(ser_to_net.control_messages)
                    while ((ST_CONNECTED not in messages_copy)
                    and (ST_CONNECTION_FAILED not in messages_copy)
                    and (time.monotonic() <= timeout_at)):
                        ser_to_net.message_lock.wait(0.1)
                        messages_copy = set(ser_to_net.control_messages)

                    ser_to_net.control_messages.clear()
                    if ST_CONNECTION_FAILED in messages_copy:
                        sys.stderr.write('ERROR: Connection failed on the other side\n')
                        client_socket.close()
                        continue
                    if ST_CONNECTED not in messages_copy:
                        sys.stderr.write('ERROR: Connection timeout on the other side\n')
                        client_socket.close()
                        continue

            else:
                # Await connect message from the other side of the serial cable
                with ser_to_net.message_lock:
                    while ST_CONNECT not in ser_to_net.control_messages:
                        ser_to_net.message_lock.wait()
                    ser_to_net.control_messages.clear()

                host, port = args.client.split(':')
                sys.stderr.write("Opening connection to {}:{}...\n".format(host, port))
                client_socket = socket.socket()
                client_socket.settimeout(args.timeout)
                try:
                    client_socket.connect((host, int(port)))
                except socket.error as msg:
                    sys.stderr.write('WARNING: {}\n'.format(msg))
                    # Tell the other side that the connection failed
                    ser.write(ST_ESCAPE + ST_CONNECTION_FAILED)
                    continue
                sys.stderr.write('Connected\n')
                client_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

                with ser_to_net.socket_lock:
                    ser_to_net.socket = client_socket

                ser.write(ST_NUL + ST_ESCAPE + ST_CONNECTED)

            # Start code indicates that the next bytes should be relayed
            ser.write(ST_ESCAPE + ST_START)
            try:
                # enter network <-> serial loop
                client_socket.settimeout(0.1)
                while True:
                    with ser_to_net.message_lock:
                        if ST_DISCONNECTED in ser_to_net.control_messages:
                            ser_to_net.control_messages.clear()
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
            except Disconnected:
                pass
            finally:
                with ser_to_net.socket_lock:
                    ser_to_net.socket = None

                sys.stderr.write('Disconnected\n')
                client_socket.close()
                ser.write(ST_ESCAPE + ST_DISCONNECTED)

    except KeyboardInterrupt:
        pass

    sys.stderr.write('\n--- exit ---\n')
    serial_worker.stop()
