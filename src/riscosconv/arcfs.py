"""
Native reader for ArcFS archives.

Based on the ArcFS support in nspark (arcfs.c, compress.c, pack.c, crc.c)
by Andrew Brooks, David Duplain et al.

Archive layout (all words little-endian):

  0   'Archive\\0'
  8   header length (bytes of entry records, 36 bytes each)
  12  data start offset
  16  ArcFS version (x100) needed to read
  20  ArcFS version (x100) needed to write
  24  archive format version (0)
  28  17 reserved words
  96  entry records

Each 36 byte entry record:

  0   info byte (0 = end of dir, 1 = deleted, else compression method)
  1   name (11 bytes, NUL terminated/padded)
  12  original length
  16  load address
  20  exec address
  24  attributes: bits 0-7 RISC OS attributes, 8-15 LZW max bits, 16-31 CRC
  28  compressed length
  32  info word: bit 31 set for directories, bits 0-30 offset of data from data start

Directories are stored inline: a directory entry is followed by the entries
it contains and then an end of dir entry.
"""

from datetime import datetime
from io import BytesIO
import os
import struct
from typing import IO, Generator, Optional, Tuple

from .ro_file_meta import DiscImageBase, FileMeta, RiscOsFileMeta
from .riscos_path import PureRiscOsPath, as_ro_path


ARCFS_MAGIC = b'Archive\x00'
ARCFS_MAX_VERSION = 40
MAIN_HEADER_LEN = 96
ENTRY_LEN = 36

AFS_ENDDIR = 0x00
AFS_DELETED = 0x01
AFS_STORE = 0x82
AFS_PACK = 0x83
AFS_CRUNCH = 0x88
AFS_SQUASH = 0x89
AFS_COMPRESS = 0xff

RUNMARK = 0x90
SQUASH_BITS = 13


class ArcFSError(Exception):
    pass


def _make_crc_table():
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ 0xa001 if crc & 1 else crc >> 1
        table.append(crc)
    return table

_CRC_TABLE = _make_crc_table()

def arc_crc16(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = (crc >> 8) ^ _CRC_TABLE[(crc ^ b) & 0xff]
    return crc


def unpack_rle(data: bytes) -> bytes:
    """Undo ARC run-length encoding: <byte> 0x90 <count> repeats byte count times, 0x90 0x00 is a literal 0x90"""
    out = bytearray()
    prev = 0
    running = False
    for b in data:
        if running:
            if b == 0:
                out.append(RUNMARK)
            else:
                out.extend(bytes((prev,)) * (b - 1))
            running = False
        elif b == RUNMARK:
            running = True
        else:
            prev = b
            out.append(b)
    return bytes(out)


def uncompress_lzw(data: bytes, maxbits: int) -> bytes:
    """
    Decompress Unix compress style LZW data (no header), as used by ARC/ArcFS
    crunch, squash and compress methods. Codes are read in groups of n_bits bytes,
    discarding any leftover bits in a group whenever the code size changes.
    """
    INIT_BITS = 9
    CLEAR = 256
    FIRST = 257

    if not INIT_BITS <= maxbits <= 16:
        raise ArcFSError(f'invalid LZW max bits: {maxbits}')

    maxmaxcode = (1 << maxbits) - 1
    prefix = [0] * (1 << maxbits)
    suffix = list(range(256)) + [0] * ((1 << maxbits) - 256)

    n_bits = INIT_BITS
    maxcode = (1 << n_bits) - 1
    free_ent = FIRST
    clear_flg = False
    pos = 0
    buf_val = 0
    buf_bits = 0   # number of valid code start offsets in buffer
    offset = 0

    def getcode():
        nonlocal n_bits, maxcode, clear_flg, pos, buf_val, buf_bits, offset
        if clear_flg or offset >= buf_bits or free_ent > maxcode:
            if free_ent > maxcode:
                n_bits += 1
                maxcode = maxmaxcode if n_bits == maxbits else (1 << n_bits) - 1
            if clear_flg:
                n_bits = INIT_BITS
                maxcode = (1 << n_bits) - 1
                clear_flg = False
            chunk = data[pos:pos + n_bits]
            if not chunk:
                return -1
            pos += len(chunk)
            buf_val = int.from_bytes(chunk, 'little')
            buf_bits = len(chunk) * 8 - (n_bits - 1)
            offset = 0
            if buf_bits <= 0:
                return -1
        code = (buf_val >> offset) & ((1 << n_bits) - 1)
        offset += n_bits
        return code

    out = bytearray()
    finchar = oldcode = getcode()
    if oldcode == -1:
        return bytes(out)
    out.append(finchar)

    stack = bytearray()
    while (code := getcode()) != -1:
        if code == CLEAR:
            for i in range(256):
                prefix[i] = 0
            clear_flg = True
            free_ent = FIRST - 1
            if (code := getcode()) == -1:
                break
        incode = code
        # KwKwK special case
        if code >= free_ent:
            stack.append(finchar)
            code = oldcode
        while code >= 256:
            if len(stack) > (1 << 16):
                raise ArcFSError('corrupt LZW data')
            stack.append(suffix[code])
            code = prefix[code]
        finchar = suffix[code]
        stack.append(finchar)
        stack.reverse()
        out += stack
        stack.clear()

        if free_ent < maxmaxcode:
            prefix[free_ent] = oldcode
            suffix[free_ent] = finchar
            free_ent += 1
        oldcode = incode

    return bytes(out)


class ArcFSEntry:
    def __init__(self, path, info_byte, length, load_addr, exec_addr, attr, complen, data_offset):
        self.path = path
        self.info_byte = info_byte
        self.length = length
        self.load_addr = load_addr
        self.exec_addr = exec_addr
        self.attr = attr & 0xff
        self.maxbits = (attr >> 8) & 0xff
        self.crc = attr >> 16
        self.complen = complen
        self.data_offset = data_offset

    @property
    def ro_meta(self):
        return RiscOsFileMeta(self.load_addr, self.exec_addr, self.attr)

    def __repr__(self):
        return f'ArcFSEntry({self.path!r} method={self.info_byte:02x} len={self.length} complen={self.complen})'


class ArcFSArchive(DiscImageBase):
    def __init__(self, fd: IO[bytes]):
        self.path = getattr(fd, 'name', 'archive')
        fd.seek(0, os.SEEK_SET)
        self.data = fd.read()
        self.entries: dict[PureRiscOsPath, ArcFSEntry] = {}
        self._parse()

    @property
    def disc_name(self):
        return os.path.basename(self.path)

    def __repr__(self):
        return 'ArcFS Archive ' + os.path.basename(self.path)

    def _parse(self):
        data = self.data
        if len(data) < MAIN_HEADER_LEN or data[0:8] != ARCFS_MAGIC:
            raise ArcFSError('not an ArcFS archive')
        header_len, data_start, version, _rw_version, fmt = struct.unpack_from('<5I', data, 8)
        if version > ARCFS_MAX_VERSION:
            raise ArcFSError(f'archive created by a newer version of ArcFS ({version // 100}.{version % 100:02d})')
        if fmt > 0:
            raise ArcFSError(f'archive format {fmt} not understood')

        dir_stack = []
        for i in range(header_len // ENTRY_LEN):
            offset = MAIN_HEADER_LEN + i * ENTRY_LEN
            if offset + ENTRY_LEN > len(data):
                raise ArcFSError('truncated ArcFS header')
            info_byte = data[offset]
            raw_name = data[offset + 1:offset + 12]
            length, load, exec_, attr, complen, info_word = struct.unpack_from('<6I', data, offset + 12)

            if info_byte == AFS_DELETED:
                continue
            if info_byte == AFS_ENDDIR:
                # some archives have a trailing end of dir marker for the root
                if dir_stack:
                    dir_stack.pop()
                continue

            # name is NUL terminated, may have junk after the terminator
            name = raw_name.split(b'\x00', 1)[0].decode('iso-8859-1')
            # '/' is a valid RISC OS leafname char, so an arcfs name keeps it.
            # ':' starts a filing-system prefix / path variable and can't
            # appear in a name either; substitute rather than reject the entry
            name = name.replace(':', '_')

            is_dir = bool(info_word >> 31) and length == 0xffffffff and complen == 0xffffffff
            if is_dir:
                dir_stack.append(name)
                continue

            path = PureRiscOsPath(*dir_stack, name)
            data_offset = (info_word & 0x7fffffff) + data_start
            self.entries[path] = ArcFSEntry(path, info_byte, length, load, exec_, attr, complen, data_offset)

    def _file_meta(self, entry: ArcFSEntry) -> FileMeta:
        ro_meta = entry.ro_meta
        ds = ro_meta.datestamp
        if not ds:
            ds = datetime.now()
        return FileMeta(ro_meta, ds, entry.length)

    def list(self) -> Generator[Tuple[str, FileMeta], None, None]:
        for path, entry in self.entries.items():
            yield path, self._file_meta(entry)

    def get_file_meta(self, path: PureRiscOsPath | str) -> FileMeta:
        return self._file_meta(self.entries[as_ro_path(path)])

    def read(self, path: PureRiscOsPath | str) -> bytes:
        entry = self.entries[as_ro_path(path)]
        comp_data = self.data[entry.data_offset:entry.data_offset + entry.complen]
        if len(comp_data) != entry.complen:
            raise ArcFSError(f'{path}: compressed data truncated')

        method = entry.info_byte
        if method == AFS_STORE:
            out = comp_data
        elif method == AFS_PACK:
            out = unpack_rle(comp_data)
        elif method == AFS_CRUNCH:
            out = unpack_rle(uncompress_lzw(comp_data, entry.maxbits))
        elif method == AFS_SQUASH:
            out = uncompress_lzw(comp_data, SQUASH_BITS)
        elif method == AFS_COMPRESS:
            out = uncompress_lzw(comp_data, entry.maxbits)
        else:
            raise ArcFSError(f'{path}: unsupported compression method 0x{method:02x}')

        out = out[:entry.length]
        if len(out) != entry.length:
            raise ArcFSError(f'{path}: expected {entry.length} bytes, got {len(out)}')
        # ArcFS sometimes records a CRC of 0, which means no CRC was stored
        if entry.crc and arc_crc16(out) != entry.crc:
            raise ArcFSError(f'{path}: CRC check failed')
        return out

    def open(self, path: PureRiscOsPath | str) -> Optional[IO[bytes]]:
        if as_ro_path(path) not in self.entries:
            return None
        return BytesIO(self.read(path))
