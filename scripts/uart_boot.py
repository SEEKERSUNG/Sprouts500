#!/usr/bin/env python3
"""Download a raw Sprouts500 application through the resident UART bootloader.

Only the Python standard library is required.  Typical use:
  python3 scripts/uart_boot.py --port /dev/ttyUSB0 --image build/fpga/app/app.bin
"""

import argparse
import os
import select
import struct
import sys
import termios
import time
import tty
import zlib
from pathlib import Path

MAGIC = b"SB"
APP_HEADER = 0x800
APP_ENTRY = 0x810
APP_LIMIT = 0x7FF0
APP_MAX = APP_LIMIT - APP_ENTRY
APP_MAGIC = 0x53555031
BOOT_VERSION = 1

CMD_INFO = 0x01
CMD_WRITE = 0x02
CMD_VERIFY = 0x03
CMD_RUN = 0x04

STATUS = {
    0: "OK",
    1: "CRC mismatch",
    2: "address/length out of range",
    3: "unknown command",
    4: "bad frame",
    5: "no valid application",
    6: "receive timeout",
}


class BootError(RuntimeError):
    pass


def crc32(data):
    return zlib.crc32(data) & 0xFFFFFFFF


def make_header(image, entry=APP_ENTRY):
    if not image or len(image) > APP_MAX:
        raise BootError(f"image size must be 1..{APP_MAX} bytes, got {len(image)}")
    if entry != APP_ENTRY or (entry & 1):
        raise BootError(f"entry must be the aligned boot-app address 0x{APP_ENTRY:x}")
    return struct.pack("<IIII", APP_MAGIC, len(image), entry, crc32(image))


def make_package(image, entry=APP_ENTRY):
    return make_header(image, entry) + image


def make_frame(command, sequence, address=0, payload=b""):
    if len(payload) > 256:
        raise BootError("a protocol payload is limited to 256 bytes")
    body = struct.pack("<BBIHH", command, sequence & 0xFF, address, len(payload), 0)
    body += payload
    return MAGIC + body + struct.pack("<I", crc32(body))


def parse_frame(frame):
    if len(frame) < 16 or frame[:2] != MAGIC:
        raise BootError("short frame or bad magic")
    command, sequence, address, length, reserved = struct.unpack("<BBIHH", frame[2:12])
    if reserved or len(frame) != 16 + length:
        raise BootError("bad reserved bits or frame length")
    body = frame[2:-4]
    if crc32(body) != struct.unpack("<I", frame[-4:])[0]:
        raise BootError("frame CRC mismatch")
    return command, sequence, address, frame[12:-4]


class SerialPort:
    def __init__(self, name):
        self.fd = os.open(name, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            attrs = termios.tcgetattr(self.fd)
            tty.setraw(self.fd, termios.TCSANOW)
            attrs = termios.tcgetattr(self.fd)
            attrs[2] |= termios.CLOCAL | termios.CREAD
            attrs[2] &= ~termios.CRTSCTS
            attrs[2] &= ~termios.CSIZE
            attrs[2] |= termios.CS8
            attrs[4] = termios.B115200
            attrs[5] = termios.B115200
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
            termios.tcflush(self.fd, termios.TCIOFLUSH)
        except Exception:
            os.close(self.fd)
            raise

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def write(self, data):
        view = memoryview(data)
        while view:
            _, writable, _ = select.select([], [self.fd], [], 1.0)
            if not writable:
                raise BootError("serial write timeout")
            count = os.write(self.fd, view)
            view = view[count:]

    def read_byte(self, deadline):
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BootError("serial response timeout")
            readable, _, _ = select.select([self.fd], [], [], remaining)
            if readable:
                data = os.read(self.fd, 1)
                if data:
                    return data[0]


class Bootloader:
    def __init__(self, port, retries=3):
        self.serial = SerialPort(port)
        self.retries = retries
        self.sequence = 0

    def close(self):
        self.serial.close()

    def next_sequence(self):
        self.sequence = (self.sequence + 1) & 0xFF
        return self.sequence

    def _read_response(self, command, sequence, timeout):
        deadline = time.monotonic() + timeout
        have_magic = False
        while True:
            value = self.serial.read_byte(deadline)
            if not have_magic:
                have_magic = value == MAGIC[0]
                continue
            if value == MAGIC[1]:
                break
            have_magic = value == MAGIC[0]

        header = bytearray()
        for _ in range(10):
            header.append(self.serial.read_byte(deadline))
        _, _, _, length, _ = struct.unpack("<BBIHH", header)
        if length > 256:
            raise BootError("response payload is too large")
        tail = bytearray()
        for _ in range(length + 4):
            tail.append(self.serial.read_byte(deadline))
        frame = MAGIC + bytes(header + tail)
        rsp_cmd, rsp_seq, _, payload = parse_frame(frame)
        if rsp_cmd != (command | 0x80) or rsp_seq != sequence:
            raise BootError(
                f"unexpected response cmd=0x{rsp_cmd:02x} seq={rsp_seq}")
        if len(payload) < 4:
            raise BootError("response has no status")
        status = struct.unpack("<I", payload[:4])[0]
        if status:
            raise BootError(STATUS.get(status, f"bootloader status {status}"))
        return payload

    def request(self, command, address=0, payload=b"", timeout=2.0):
        sequence = self.next_sequence()
        frame = make_frame(command, sequence, address, payload)
        last_error = None
        for _ in range(self.retries):
            try:
                self.serial.write(frame)
                return self._read_response(command, sequence, timeout)
            except BootError as error:
                last_error = error
        raise BootError(f"command 0x{command:02x} failed after retries: {last_error}")

    def download(self, package, chunk_size=256, run=True):
        if len(package) < 17:
            raise BootError("package is empty")
        header, image = package[:16], package[16:]
        magic, length, entry, expected_crc = struct.unpack("<IIII", header)
        if magic != APP_MAGIC or length != len(image) or expected_crc != crc32(image):
            raise BootError("package header does not match its application bytes")

        info = self.request(CMD_INFO)
        if len(info) != 20:
            raise BootError("INFO response has the wrong length")
        _, version, app_header, app_entry, app_max = struct.unpack("<IIIII", info)
        if (version, app_header, app_entry, app_max) != (
                BOOT_VERSION, APP_HEADER, APP_ENTRY, APP_MAX):
            raise BootError("incompatible bootloader memory layout or version")
        if len(image) > app_max:
            raise BootError("application does not fit the boot layout")

        # Invalidate first; write the trusted header only after every data byte.
        self.request(CMD_WRITE, APP_HEADER, b"\0" * 4)
        next_report = 1024
        for offset in range(0, len(image), chunk_size):
            part = image[offset:offset + chunk_size]
            self.request(CMD_WRITE, entry + offset, part)
            done = offset + len(part)
            if done >= next_report or done == len(image):
                print(f"downloaded {done}/{len(image)} bytes", flush=True)
                next_report += 1024
        self.request(CMD_WRITE, APP_HEADER, header)

        verify = self.request(CMD_VERIFY)
        if len(verify) != 8 or struct.unpack("<I", verify[4:])[0] != expected_crc:
            raise BootError("device CRC differs from the host image")
        print(f"verified CRC32 0x{expected_crc:08x}", flush=True)
        if run:
            self.request(CMD_RUN)
            print(f"application started at 0x{entry:08x}", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="raw app.bin linked at 0x810")
    parser.add_argument("--port", help="Linux serial device, for example /dev/ttyUSB0")
    parser.add_argument("--pack-only", action="store_true",
                        help="write 16-byte header + app and do not open a serial port")
    parser.add_argument("--output", help="package path required by --pack-only")
    parser.add_argument("--entry", type=lambda value: int(value, 0), default=APP_ENTRY)
    parser.add_argument("--chunk", type=int, default=256, help="payload size, 1..256")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--no-run", action="store_true", help="stop after CRC verification")
    args = parser.parse_args(argv)

    try:
        image = Path(args.image).read_bytes()
        package = make_package(image, args.entry)
        if args.pack_only:
            if not args.output:
                raise BootError("--pack-only requires --output")
            Path(args.output).write_bytes(package)
            print(f"PACKAGE={args.output} app_bytes={len(image)} "
                  f"package_bytes={len(package)} crc32=0x{crc32(image):08x}")
            return 0
        if not args.port:
            raise BootError("--port is required unless --pack-only is used")
        if not 1 <= args.chunk <= 256:
            raise BootError("--chunk must be in 1..256")
        if args.retries < 1:
            raise BootError("--retries must be positive")
        loader = Bootloader(args.port, args.retries)
        try:
            loader.download(package, args.chunk, not args.no_run)
        finally:
            loader.close()
        return 0
    except (BootError, OSError, termios.error) as error:
        print(f"uart_boot: {error}", file=sys.stderr)
        if not args.pack_only:
            print("Hold BOOT0 low, reset the board, then retry while the downloader is active.",
                  file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
