"""
Native reader for ArcFS archives.

Based on the ArcFS support in nspark (arcfs.c) by Andrew Brooks, David
Duplain et al.  The compression and CRC codecs shared with Spark live in
riscosconv.codec.

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

from .codec import CodecError, SQUASH_BITS, arc_crc16, unpack_rle, uncompress_lzw
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
            raise CodecError('not an ArcFS archive')
        header_len, data_start, version, _rw_version, fmt = struct.unpack_from('<5I', data, 8)
        if version > ARCFS_MAX_VERSION:
            raise CodecError(f'archive created by a newer version of ArcFS ({version // 100}.{version % 100:02d})')
        if fmt > 0:
            raise CodecError(f'archive format {fmt} not understood')

        dir_stack = []
        for i in range(header_len // ENTRY_LEN):
            offset = MAIN_HEADER_LEN + i * ENTRY_LEN
            if offset + ENTRY_LEN > len(data):
                raise CodecError('truncated ArcFS header')
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
            raise CodecError(f'{path}: compressed data truncated')

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
            raise CodecError(f'{path}: unsupported compression method 0x{method:02x}')

        out = out[:entry.length]
        if len(out) != entry.length:
            raise CodecError(f'{path}: expected {entry.length} bytes, got {len(out)}')
        # ArcFS sometimes records a CRC of 0, which means no CRC was stored
        if entry.crc and arc_crc16(out) != entry.crc:
            raise CodecError(f'{path}: CRC check failed')
        return out

    def open(self, path: PureRiscOsPath | str) -> Optional[IO[bytes]]:
        if as_ro_path(path) not in self.entries:
            return None
        return BytesIO(self.read(path))
