#!/bin/bash

import subprocess, tempfile, time, socket
from pathlib import Path

PROGRAM = ["python", "../serialtcp.py"]
DEVICE_0 = "/dev/ttyUSB0"
DEVICE_1 = "/dev/ttyUSB1"
SPEED = 115200 * 8
SSH_SERVER = "localhost:22"
LOCAL_PORT = 1234
TEST_DATA_SIZE = 1 << 20
TEST_CYCLES = 16


def wait_for_server() -> None:
    end_time = time.monotonic() + 10.0
    while time.monotonic() < end_time:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1.0)
            s.connect(("localhost", LOCAL_PORT))
            s.close()
            return  # Connected!
        except socket.error:
            # Try again
            time.sleep(0.5)

    raise Exception("Unable to connect to local port for testing!")

def main() -> None:
    print("Set up both ends of the connection...", flush=True)
    client = subprocess.Popen(PROGRAM + ["-c", SSH_SERVER, DEVICE_0, str(SPEED)], stdin=subprocess.DEVNULL)
    server = subprocess.Popen(PROGRAM + ["-s", str(LOCAL_PORT), DEVICE_1, str(SPEED)], stdin=subprocess.DEVNULL)

    try:
        with open("/dev/urandom", "rb") as fd:
            input_data = fd.read(TEST_DATA_SIZE)

        with tempfile.TemporaryDirectory() as td:
            input_file = Path(td) / "input"
            output_file = Path(td) / "output"
            with open(input_file, "wb") as fd:
                fd.write(input_data)

            print(flush=True, end="")
            wait_for_server()
            print(flush=True, end="")

            # The main part of the test
            start_time = time.monotonic()
            for i in range(TEST_CYCLES):
                print(f"Test cycle {i}",  flush=True)
                with open(output_file, "wb") as fd:
                    rc = subprocess.call(["ssh", "-p", str(LOCAL_PORT), "localhost", "cat"],
                                stdin=open(input_file, "rb"), stdout=fd)

                with open(output_file, "rb") as fd:
                    output_data = fd.read()

                print(f"Transferred {len(output_data)} bytes and rc = {rc}", flush=True)
                assert input_data == output_data
                assert rc == 0

            end_time = time.monotonic()

            # Additional fast connect/disconnect tests
            for i in range(3):
                wait_for_server()

        print("Transfer rate: {:1.0f} bps (actual data) at speed {:1.0f} bps (over the wire)".format(
                (TEST_DATA_SIZE * TEST_CYCLES * 8) / (end_time - start_time),
                SPEED))
    except KeyboardInterrupt:
        pass
    finally:
        client.kill()
        server.kill()

if __name__ == "__main__":
    main()
