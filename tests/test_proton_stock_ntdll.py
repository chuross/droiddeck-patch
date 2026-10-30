import hashlib
import os
from pathlib import Path
import runpy
import struct
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RECIPE = runpy.run_path(str(ROOT / "overlay/proton-ntcontext/stock_ntdll.py"))


LAYOUTS = list({id(layout): layout for layout in RECIPE["LAYOUTS"].values()}.values())


class OriginalDllGuardTest(unittest.TestCase):
    def fixture(self, layout=LAYOUTS[0]):
        data = bytearray(2162688)
        struct.pack_into("<I", data, RECIPE["CODEMAP_LENGTH_OFFSET"], layout["code_length"])
        struct.pack_into("<II", data, RECIPE["REDIRECTION_OFFSET"] - 4, layout["source"], layout["entry"])
        struct.pack_into("<Q", data, RECIPE["SYSCALL_POINTER_OFFSET"], 0x180000000 + layout["entry"])
        data[layout["entry"]:layout["entry"] + 4] = bytes.fromhex("f353bda9")
        struct.pack_into("<i", data, layout["entry"] - 4, layout["prefix"])
        return bytes(data)

    def inspected(self, original, layout=LAYOUTS[0]):
        return patch.dict(RECIPE["LAYOUTS"], {hashlib.sha256(original).hexdigest(): layout})

    def test_only_leaf_guard_and_its_routing_change(self):
        for layout in LAYOUTS:
            with self.subTest(entry=hex(layout["entry"])):
                self.check_guard(layout)

    def check_guard(self, layout):
        original = self.fixture(layout)
        guard_rva = layout["guard_prefix"] + 4
        with self.inspected(original, layout):
            candidate = RECIPE["patch"](original)
            report = RECIPE["verify"](original, candidate)
            self.assertEqual(report["original_sha256"], hashlib.sha256(original).hexdigest())
            permitted = {i for start, length in report["changed_ranges"] for i in range(start, start + length)}
            self.assertEqual(len(candidate), len(original))
            self.assertTrue(all(i in permitted for i, (a, b) in enumerate(zip(original, candidate)) if a != b))
            self.assertEqual(len(permitted), 36)
            entry_prefix = struct.unpack_from("<i", candidate, layout["guard_prefix"])[0]
            self.assertEqual(entry_prefix & 1, 1)
            self.assertEqual(guard_rva + (entry_prefix & ~1), layout["entry"] + (layout["prefix"] & ~1))
            self.assertEqual(struct.unpack_from("<I", candidate, RECIPE["CODEMAP_LENGTH_OFFSET"])[0],
                             layout["code_length"] + 0x14)
            self.assertEqual(struct.unpack_from("<Q", candidate, RECIPE["SYSCALL_POINTER_OFFSET"])[0],
                             0x180000000 + guard_rva)
            guard = struct.unpack_from("<4I", candidate, guard_rva)
            self.assertEqual(guard[0] & 0xff00001f, 0xb5000001)  # cbnz x1
            displacement = (guard[0] >> 5) & 0x7ffff
            if displacement & 0x40000:
                displacement -= 0x80000
            self.assertEqual(guard_rva + displacement * 4, layout["entry"])
            self.assertEqual(guard[1:], (0x528001a0, 0x72b80000, 0xd65f03c0))
            changed = bytearray(candidate)
            changed[0x100] ^= 1
            with self.assertRaisesRegex(ValueError, "outside"):
                RECIPE["verify"](original, bytes(changed))

    def test_uninspected_or_already_patched_dll_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported original"):
            RECIPE["patch"](self.fixture())

    def test_occupied_padding_or_changed_routing_is_rejected(self):
        for offset in (LAYOUTS[0]["guard_prefix"] + 4, RECIPE["CODEMAP_LENGTH_OFFSET"], RECIPE["REDIRECTION_OFFSET"]):
            original = bytearray(self.fixture())
            original[offset] ^= 1
            with self.inspected(original):
                with self.assertRaisesRegex(ValueError, "layout"):
                    RECIPE["patch"](original)

    @unittest.skipUnless(os.environ.get("DROIDDECK_STOCK_NTDLL"), "original release DLL not supplied")
    def test_inspected_release_binary(self):
        original = Path(os.environ["DROIDDECK_STOCK_NTDLL"]).read_bytes()
        candidate = RECIPE["patch"](original)
        expected = {
            "6a185abe76c901c9ae64f9500a952a93665efd1ad19ac0eea3e78790541f21b9":
                "230be42fe184bf28932f84707c42d057560abae512b961f434dd79ce7c30fa59",
            "4df3958ca291b682ab6e9f6463ee11465339bcca0eac4917200f5f33a9beb954":
                "5f1731950f5424ac8fec3755ff978c7aea7749012a7a7efab6b40b99e088ec15",
            "18e970a8d2fdde2496d197b52eb80e87626e169a00b986dbe2c3d918839cfaf6":
                "e88a7d5a0c83287b2b2e77ae8e9c997c539ff9ea55aef57e0df3b2d0721d57ac",
        }
        report = RECIPE["verify"](original, candidate)
        self.assertEqual(report["original_sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(report["patched_sha256"], expected[report["original_sha256"]])
