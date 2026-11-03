#!/usr/bin/env python3
"""Standard-library unit tests for the UART boot image/protocol encoder."""

import importlib.util
import struct
import unittest
import zlib
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "uart_boot.py"
SPEC = importlib.util.spec_from_file_location("uart_boot", MODULE_PATH)
uart_boot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(uart_boot)


class UartBootTests(unittest.TestCase):
    def test_crc32_known_vector(self):
        self.assertEqual(uart_boot.crc32(b"123456789"), 0xCBF43926)
        self.assertEqual(uart_boot.crc32(b"123456789"), zlib.crc32(b"123456789"))

    def test_package_header(self):
        image = bytes(range(256)) * 3 + b"tail"
        package = uart_boot.make_package(image)
        magic, length, entry, checksum = struct.unpack("<IIII", package[:16])
        self.assertEqual(magic, uart_boot.APP_MAGIC)
        self.assertEqual(length, len(image))
        self.assertEqual(entry, uart_boot.APP_ENTRY)
        self.assertEqual(checksum, uart_boot.crc32(image))
        self.assertEqual(package[16:], image)

    def test_frame_round_trip(self):
        payload = bytes(range(256))
        frame = uart_boot.make_frame(uart_boot.CMD_WRITE, 0xA5, 0x12345678, payload)
        command, sequence, address, decoded = uart_boot.parse_frame(frame)
        self.assertEqual((command, sequence, address), (2, 0xA5, 0x12345678))
        self.assertEqual(decoded, payload)

    def test_frame_crc_is_mandatory(self):
        frame = bytearray(uart_boot.make_frame(2, 1, 0x810, b"abc"))
        frame[-1] ^= 0x80
        with self.assertRaisesRegex(uart_boot.BootError, "CRC"):
            uart_boot.parse_frame(frame)

    def test_bad_lengths_are_rejected(self):
        frame = bytearray(uart_boot.make_frame(2, 1, 0x810, b"abc"))
        frame[8] = 4
        with self.assertRaisesRegex(uart_boot.BootError, "length"):
            uart_boot.parse_frame(frame)
        with self.assertRaises(uart_boot.BootError):
            uart_boot.make_frame(2, 1, 0x810, bytes(257))
        with self.assertRaises(uart_boot.BootError):
            uart_boot.make_package(b"")
        with self.assertRaises(uart_boot.BootError):
            uart_boot.make_package(bytes(uart_boot.APP_MAX + 1))


if __name__ == "__main__":
    unittest.main()
