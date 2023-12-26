serialtcp
=========

This program redirects a TCP connection via a serial port.

The program should run on both sides of a serial connection:

- On one side, it acts as a TCP server, and any connection to its TCP port
  will be forwarded across the serial cable.

- On the other side, it acts as a TCP client, and any connection via the
  serial cable will be forwarded to a specified IP address and port number.

Usage
=====

Use this program to provide access to a TCP service
without needing to establish a network connection.

For example, on one side, acting as a TCP server on port 1234:

    python serialtcp.py --server 1234 /dev/ttyUSB0
    
And on the other side, acting as a TCP client which will connect to 192.168.0.1 on port 22:

    python serialtcp.py --client 192.168.0.1:22 /dev/ttyUSB0 

When both copies of the program are running, they will synchronise
over the serial cable. A connection to port 1234
will be forwarded across the serial cable to 192.168.0.1:22.

Limitations
===========

No security measures are implemented. Anyone can remotely connect
to this service, but by default,
the server port only binds to localhost interfaces.

Only one connection at once is supported. When the connection is terminated
it waits for the next connection.

The protocol uses escape codes to represent events such as connection and
disconnection, and therefore the connection works much like a TCP stream,
but TCP error conditions and settings like NODELAY are not carried across the link.

There is no support for other protocols (e.g. UDP).

If you need multiple simultaneous connections, proper handling of error conditions,
authentication or support for other protocols you should consider using PPP or SLIP.

You cannot use this program to simply expose a serial port as a TCP service
which can be used via `telnet` or `netcat`. If this is what you need, consider using
tcp\_serial\_redirect.py instead (see Acknowledgments below). The original
tcp\_serial\_redirect.py is not suitable for forwarding most TCP connections (e.g. SSH, HTTP)
because each network connection is established on startup, rather than when a client connects.

Acknowledgments
===============

This program is based on an example from `pyserial`
forked from [the pyserial repo](https://github.com/pyserial/pyserial/blob/master/examples/tcp_serial_redirect.py)
and written by Chris Liechti. Thanks, Chris, for your work on `pyserial`!

See https://github.com/pyserial/pyserial

