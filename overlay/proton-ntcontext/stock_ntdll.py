"""Add a leaf ARM64EC NULL guard to the verified original release DLLs."""
import hashlib
import struct

# Inspected against the released ARM64X images, not rebuilt DLLs. The native
# export and both architectures' original code, relocations and unwind records
# remain intact. Fail closed for every other upstream binary.
CODEMAP_LENGTH_OFFSET = 0x13ac91
REDIRECTION_OFFSET = 0x1804bc
SYSCALL_POINTER_OFFSET = 0x130460
# guard_prefix: end of the ARM64EC code range, followed by zero-filled padding.
# entry: NtGetContextThread's ARM64EC body; source: its x64 syscall thunk (0x97).
# prefix: the entry's signed relative entry-thunk reference.
LAYOUT_11_7 = {"guard_prefix": 0xf2374, "entry": 0xcdb00, "code_length": 0x6e374,
               "source": 0xf42e0, "prefix": 0x1db01}
LAYOUTS = {
    "6a185abe76c901c9ae64f9500a952a93665efd1ad19ac0eea3e78790541f21b9": LAYOUT_11_7,
    # 11-9.1 differs from 11-7.5 only in the COFF/debug timestamps at
    # 0x80 and 0x13ac54. Code, entry thunk, padding and routing are identical.
    "4df3958ca291b682ab6e9f6463ee11465339bcca0eac4917200f5f33a9beb954": LAYOUT_11_7,
    # 11-11.9 grows the ARM64EC range by 0x1020 and moves the entry and its
    # thunk by 0xf70 and 0x1020. The metadata offsets, syscall number, prologue
    # and entry-thunk code are unchanged, and the entry still lacks a NULL check.
    "18e970a8d2fdde2496d197b52eb80e87626e169a00b986dbe2c3d918839cfaf6":
        {"guard_prefix": 0xf3394, "entry": 0xcea70, "code_length": 0x6f394, "source": 0xf52e0, "prefix": 0x1dbb1},
}


def layout(data):
    found = LAYOUTS.get(hashlib.sha256(data).hexdigest())
    if found is None:
        raise ValueError("unsupported original ntdll; inspect upstream DLL before updating the recipe")
    return found


def patch(data):
    known = layout(data)
    guard_prefix, entry = known["guard_prefix"], known["entry"]
    guard_rva = guard_prefix + 4
    # The existing ARM64EC code range ends here. Zero-filled executable padding
    # follows it, before the next x64 code range. Extend only this range by the
    # 4-byte entry descriptor and 16-byte guard. No existing function is moved
    # or overwritten.
    if (data[guard_prefix:guard_prefix + 20] != bytes(20)
            or struct.unpack_from("<I", data, CODEMAP_LENGTH_OFFSET)[0] != known["code_length"]
            or struct.unpack_from("<II", data, REDIRECTION_OFFSET - 4) != (known["source"], entry)
            or struct.unpack_from("<Q", data, SYSCALL_POINTER_OFFSET)[0] != 0x180000000 + entry
            or data[entry:entry + 4] != bytes.fromhex("f353bda9")):
        raise ValueError("original ARM64EC layout differs from inspected DLL")
    entry_prefix = struct.unpack_from("<i", data, entry - 4)[0]
    if entry_prefix != known["prefix"]:
        raise ValueError("original ARM64EC entry thunk changed")
    delta = (entry - guard_rva) // 4
    if (entry - guard_rva) % 4 or not -(1 << 18) <= delta < (1 << 18):
        raise ValueError("ARM64EC guard branch is out of range")
    # cbnz x1, original entry; mov w0, 13; movk w0, 0xc000, lsl 16; ret.
    # This is a leaf with no stack adjustment, so the original unwind table is
    # valid without adding a frame. Non-NULL calls execute the original prologue.
    guard = struct.pack("<4I", 0xb5000001 | ((delta & 0x7ffff) << 5),
                        0x528001a0, 0x72b80000, 0xd65f03c0)
    result = bytearray(data)
    # ARM64EC entry points carry a signed relative entry-thunk reference in
    # their preceding word. FEX uses it to marshal x64 arguments. Reuse the
    # original NtGetContextThread thunk, with the corrected offset.
    entry_thunk = entry + (entry_prefix & ~1)
    struct.pack_into("<i", result, guard_prefix, (entry_thunk - guard_rva) | 1)
    result[guard_rva:guard_rva + 16] = guard
    struct.pack_into("<I", result, CODEMAP_LENGTH_OFFSET, known["code_length"] + 0x14)
    struct.pack_into("<I", result, REDIRECTION_OFFSET, guard_rva)
    # Wine recognizes x64 syscall stubs through arm64ec_syscalls as well as
    # CHPE redirections. Route both to the guard, retaining the existing DIR64
    # relocation for this absolute pointer (including rebased DLL loads).
    struct.pack_into("<Q", result, SYSCALL_POINTER_OFFSET, 0x180000000 + guard_rva)
    return bytes(result)


def verify(original, candidate):
    if patch(original) != candidate:
        raise ValueError("ntdll contains changes outside the original DLL NULL guard")
    known = layout(original)
    guard_prefix = known["guard_prefix"]
    return {"method": "original-dll-arm64ec-leaf-guard", "original_sha256": hashlib.sha256(original).hexdigest(),
            "patched_sha256": hashlib.sha256(candidate).hexdigest(),
            "guard_rva": guard_prefix + 4, "original_entry_rva": known["entry"],
            "changed_ranges": [[guard_prefix, 20], [CODEMAP_LENGTH_OFFSET, 4], [REDIRECTION_OFFSET, 4],
                               [SYSCALL_POINTER_OFFSET, 8]],
            "maximum_modified_bytes": 36}
