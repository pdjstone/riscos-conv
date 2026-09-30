"""
Helpers to generate minimal ArcFS archives for tests.

Includes encoders for each ArcFS storage method, written independently of
the decoders in riscosconv.arcfs (they are ports of nspark's pack.c and
classic Unix compress), so that tests exercise real round trips.
"""

from dataclasses import dataclass, field
from datetime import datetime
import struct
from typing import Optional, Union

from riscosconv.arcfs import (
    AFS_COMPRESS, AFS_CRUNCH, AFS_DELETED, AFS_ENDDIR, AFS_PACK, AFS_SQUASH, AFS_STORE,
    ARCFS_MAGIC, ENTRY_LEN, MAIN_HEADER_LEN, SQUASH_BITS, arc_crc16,
)
from riscosconv.ro_file_meta import RISC_OS_EPOCH, make_load_exec


RUNMARK = 0x90
DEFAULT_TIMESTAMP = datetime(1998, 6, 14, 17, 0, 52, 120000)


def pack_rle(data: bytes) -> bytes:
    """ARC run-length encoding, ported from nspark pack.c"""
    out = bytearray()
    if not data:
        return bytes(out)

    def write_ncr(byte, count):
        if count > 1:
            out.extend((byte, RUNMARK, count))
        elif byte == RUNMARK:
            out.extend((RUNMARK, 0))
        else:
            out.append(byte)

    prev = data[0]
    count = 1
    for byte in data[1:]:
        if prev == RUNMARK:
            write_ncr(prev, 1)
            count = 1
        elif byte == prev and count < 254:
            count += 1
        else:
            write_ncr(prev, count)
            count = 1
        prev = byte
    write_ncr(prev, count)
    return bytes(out)


def compress_lzw(data: bytes, maxbits: int, clear_when_full: bool = True) -> bytes:
    """
    Unix compress style LZW (without the 3 byte header). Codes are written in
    groups of 8, and a partial group is padded out to n_bits bytes whenever the
    code size changes. If clear_when_full is set, a CLEAR code is emitted each
    time the string table fills up.
    """
    INIT_BITS = 9
    CLEAR = 256
    FIRST = 257

    out = bytearray()
    maxmaxcode = (1 << maxbits) - 1
    n_bits = INIT_BITS
    maxcode = (1 << n_bits) - 1
    free_ent = FIRST
    clear_flg = False
    group = []

    def flush_group(pad):
        nonlocal group
        val = 0
        for i, code in enumerate(group):
            val |= code << (i * n_bits)
        nbytes = n_bits if pad else (len(group) * n_bits + 7) // 8
        out.extend(val.to_bytes(n_bits, 'little')[:nbytes])
        group = []

    def output(code):
        nonlocal n_bits, maxcode, clear_flg
        group.append(code)
        if len(group) == 8:
            flush_group(pad=True)
        if free_ent > maxcode or clear_flg:
            if group:
                flush_group(pad=True)
            if clear_flg:
                n_bits = INIT_BITS
                clear_flg = False
            else:
                n_bits += 1
            maxcode = maxmaxcode if n_bits == maxbits else (1 << n_bits) - 1

    if not data:
        return bytes(out)

    table = {}
    ent = data[0]
    for c in data[1:]:
        key = (ent, c)
        if key in table:
            ent = table[key]
            continue
        output(ent)
        ent = c
        if free_ent < maxmaxcode:
            table[key] = free_ent
            free_ent += 1
        elif clear_when_full:
            table.clear()
            free_ent = FIRST
            clear_flg = True
            output(CLEAR)
    output(ent)
    if group:
        flush_group(pad=False)
    return bytes(out)


def encode(data: bytes, method: int, maxbits: int) -> bytes:
    if method == AFS_STORE:
        return data
    if method == AFS_PACK:
        return pack_rle(data)
    if method == AFS_CRUNCH:
        return compress_lzw(pack_rle(data), maxbits)
    if method == AFS_SQUASH:
        return compress_lzw(data, SQUASH_BITS)
    if method == AFS_COMPRESS:
        return compress_lzw(data, maxbits)
    raise ValueError(f'unknown method {method:x}')


def ro_timestamp(dt: datetime) -> int:
    return int((dt - RISC_OS_EPOCH).total_seconds() * 100)


def name_field(name: Union[str, bytes]) -> bytes:
    if isinstance(name, str):
        name = name.encode('iso-8859-1')
    assert len(name) <= 11, name
    return name.ljust(11, b'\x00')


@dataclass
class ArcFSFile:
    name: Union[str, bytes]
    data: bytes = b''
    filetype: int = 0xfff
    method: int = AFS_COMPRESS
    maxbits: int = 12
    timestamp: datetime = DEFAULT_TIMESTAMP
    attr: int = 0x03
    # None = calculate, 0 = not recorded, anything else is stored as given
    crc: Optional[int] = None
    # override load/exec rather than using filetype + timestamp
    load_exec: Optional[tuple[int, int]] = None
    # override the stored compressed data
    comp_data: Optional[bytes] = None


@dataclass
class ArcFSDir:
    name: Union[str, bytes]
    entries: list = field(default_factory=list)
    timestamp: datetime = DEFAULT_TIMESTAMP


@dataclass
class ArcFSDeleted:
    name: Union[str, bytes] = 'Deleted'


@dataclass
class ArcFSEndDir:
    """An explicit end of dir marker, e.g. to make an unbalanced archive"""
    # ArcFS sometimes leaves stale data in end of dir records
    name: Union[str, bytes] = b''


def build_arcfs(entries: list, *, version: int = 40, rw_version: int = 100,
                fmt: int = 0, trailing_enddir: bool = False) -> bytes:
    """Build an ArcFS archive containing the given ArcFSFile/ArcFSDir/... entries"""
    records = []
    blobs = bytearray()

    def record(info_byte, name, length, load, exec_, attr, complen, info_word):
        records.append(struct.pack('<B11s6I', info_byte, name_field(name),
                                   length, load, exec_, attr, complen, info_word))

    def add(entry):
        if isinstance(entry, ArcFSDir):
            load, exec_ = make_load_exec(0xfff, ro_timestamp(entry.timestamp))
            record(AFS_STORE, entry.name, 0xffffffff, load, exec_, 0, 0xffffffff, 0x80000000)
            for child in entry.entries:
                add(child)
            record(AFS_ENDDIR, b'', 0, 0, 0, 0, 0, 0)
        elif isinstance(entry, ArcFSFile):
            if entry.load_exec:
                load, exec_ = entry.load_exec
            else:
                load, exec_ = make_load_exec(entry.filetype, ro_timestamp(entry.timestamp))
            comp = entry.comp_data if entry.comp_data is not None else encode(entry.data, entry.method, entry.maxbits)
            crc = arc_crc16(entry.data) if entry.crc is None else entry.crc
            maxbits = entry.maxbits if entry.method in (AFS_COMPRESS, AFS_CRUNCH) else 0
            attr = (crc << 16) | (maxbits << 8) | entry.attr
            record(entry.method, entry.name, len(entry.data), load, exec_, attr, len(comp), len(blobs))
            blobs.extend(comp)
        elif isinstance(entry, ArcFSDeleted):
            record(AFS_DELETED, entry.name, 0, 0, 0, 0, 0, 0)
        elif isinstance(entry, ArcFSEndDir):
            record(AFS_ENDDIR, entry.name, 0, 0, 0, 0, 0, 0)
        else:
            raise TypeError(entry)

    for entry in entries:
        add(entry)
    if trailing_enddir:
        add(ArcFSEndDir())

    header_len = len(records) * ENTRY_LEN
    data_start = MAIN_HEADER_LEN + header_len
    header = ARCFS_MAGIC + struct.pack('<5I', header_len, data_start, version, rw_version, fmt) + bytes(17 * 4)
    assert len(header) == MAIN_HEADER_LEN
    return header + b''.join(records) + bytes(blobs)
