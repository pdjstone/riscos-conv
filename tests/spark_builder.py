"""
Helpers to generate Spark archives for tests.

The compression encoders are shared with arcfs_builder (ports of nspark's
pack.c and classic Unix compress), written independently of the decoders in
riscosconv.arcfs so the tests exercise real round trips.
"""

from dataclasses import dataclass, field
from datetime import datetime
import struct
from typing import Optional, Union

from riscosconv.arcfs import arc_crc16, SQUASH_BITS
from riscosconv.ro_file_meta import make_load_exec
from riscosconv.spark import (
    ARCHPACK, CT_COMP, CT_CRUNCH, CT_NOTCOMP, CT_NOTCOMP2, CT_PACK, CT_SQUASH, STARTBYTE,
)

from arcfs_builder import DEFAULT_TIMESTAMP, compress_lzw, pack_rle, ro_timestamp


def encode(data: bytes, method: int, maxbits: int) -> bytes:
    if method in (CT_NOTCOMP, CT_NOTCOMP2):
        return data
    if method == CT_PACK:
        return pack_rle(data)
    if method == CT_CRUNCH:
        return bytes([maxbits]) + compress_lzw(pack_rle(data), maxbits)
    if method == CT_SQUASH:
        return compress_lzw(data, SQUASH_BITS)
    if method == CT_COMP:
        return bytes([maxbits]) + compress_lzw(data, maxbits)
    raise ValueError(f'unknown method {method:x}')


def name_field(name: Union[str, bytes]) -> bytes:
    if isinstance(name, str):
        name = name.encode('iso-8859-1')
    assert len(name) <= 13, name
    return name.ljust(13, b'\x00')


def dos_date_time(dt: datetime) -> tuple[int, int]:
    date = ((dt.year - 1980) << 9) | (dt.month << 5) | dt.day
    time = (dt.hour << 11) | (dt.minute << 5) | (dt.second // 2)
    return date, time


@dataclass
class SparkFile:
    name: Union[str, bytes]
    data: bytes = b''
    filetype: int = 0xfff
    method: int = CT_CRUNCH
    maxbits: int = 12
    timestamp: datetime = DEFAULT_TIMESTAMP
    attr: int = 0x03
    # None = calculate, anything else is stored as given
    crc: Optional[int] = None
    # override load/exec rather than using filetype + timestamp
    load_exec: Optional[tuple[int, int]] = None
    # override the stored compressed data
    comp_data: Optional[bytes] = None
    # False makes a PC style header with no load/exec/attr
    riscos: bool = True
    # override the raw DOS date/time halfwords
    dos_date_time: Optional[tuple[int, int]] = None


@dataclass
class SparkDir:
    name: Union[str, bytes]
    entries: list = field(default_factory=list)
    timestamp: datetime = DEFAULT_TIMESTAMP


@dataclass
class SparkEnd:
    """An explicit end marker, e.g. to make an unbalanced archive"""


def build_spark(entries: list, *, end_marker: bool = True) -> bytes:
    """Build a Spark archive containing the given SparkFile/SparkDir/... entries"""
    out = bytearray()

    def header(method, name, complen, dt_halfwords, crc, length, riscos_fields):
        raw_method = method | (ARCHPACK if riscos_fields else 0)
        hdr = struct.pack('<BB13sIHHH', STARTBYTE, raw_method, name_field(name), complen, *dt_halfwords, crc)
        if method > CT_NOTCOMP:
            hdr += struct.pack('<I', length)
        if riscos_fields:
            hdr += struct.pack('<3I', *riscos_fields)
        out.extend(hdr)

    def add(entry):
        if isinstance(entry, SparkDir):
            load, exec_ = make_load_exec(0xddc, ro_timestamp(entry.timestamp))
            # like real archives, the length covers the contents and end marker
            header_pos = len(out)
            header(CT_NOTCOMP2, entry.name, 0, dos_date_time(entry.timestamp), 0, 0, (load, exec_, 0))
            contents_pos = len(out)
            for child in entry.entries:
                add(child)
            out.extend((STARTBYTE, 0))
            struct.pack_into('<I', out, header_pos + 15, len(out) - contents_pos)
            struct.pack_into('<I', out, header_pos + 25, len(out) - contents_pos)
        elif isinstance(entry, SparkFile):
            comp = entry.comp_data if entry.comp_data is not None else encode(entry.data, entry.method, entry.maxbits)
            crc = arc_crc16(entry.data) if entry.crc is None else entry.crc
            dt_halfwords = entry.dos_date_time or dos_date_time(entry.timestamp)
            riscos_fields = None
            if entry.riscos:
                if entry.load_exec:
                    load, exec_ = entry.load_exec
                else:
                    load, exec_ = make_load_exec(entry.filetype, ro_timestamp(entry.timestamp))
                riscos_fields = (load, exec_, entry.attr)
            header(entry.method, entry.name, len(comp), dt_halfwords, crc, len(entry.data), riscos_fields)
            out.extend(comp)
        elif isinstance(entry, SparkEnd):
            out.extend((STARTBYTE, 0))
        else:
            raise TypeError(entry)

    for entry in entries:
        add(entry)
    if end_marker:
        out.extend((STARTBYTE, 0))
    return bytes(out)
