"""
Native reader for Spark archives.

Based on the Spark support in nspark (unarc.c, io.c, compress.c, pack.c,
store.c, crc.c, date.c) by Andrew Brooks, David Duplain et al. Spark is a
variant of the PC ARC format. The compression methods and CRC are shared with
ArcFS, so the codecs live in riscosconv.arcfs.

An archive is a sequence of entries, each a 0x1a marker byte, a header and
then the (compressed) file data. All words are little-endian:

  0   0x1a
  1   compression method (bit 7 set if the header has RISC OS metadata)
  2   name, 13 bytes, NUL terminated
  15  compressed length (word)
  19  date, MS-DOS format (halfword)
  21  time, MS-DOS format (halfword)
  23  CRC-16 of the uncompressed data (halfword)
  25  original length (word, omitted for method 1)
  29  load address (word, RISC OS headers only)
  33  exec address (word, RISC OS headers only)
  37  attributes (word, RISC OS headers only)

A directory is a stored (method 2) entry with filetype &DDC (Archive), followed
by the entries inside it and then an end marker. Its length fields cover the
contents of the directory, so they are not skipped, and like nspark any stored
&DDC entry is taken to be a directory. The archive itself ends with an end
marker: 0x1a, 0x00.

Crunch (8) and compress (0x7f) data starts with a byte giving the maximum LZW
code size; squash (9) is always 13 bits.
"""

from datetime import datetime
from io import BytesIO
import os
import struct
from typing import IO, Generator, Optional, Tuple

from .arcfs import ArcFSError, arc_crc16, uncompress_lzw, unpack_rle, SQUASH_BITS
from .ro_file_meta import RISC_OS_EPOCH, DiscImageBase, FileMeta, RiscOsFileMeta
from .riscos_path import PureRiscOsPath, as_ro_path


STARTBYTE = 0x1a
ARCHPACK = 0x80  # set in the method byte if the header has load/exec/attr

CT_NOTCOMP = 0x01
CT_NOTCOMP2 = 0x02
CT_PACK = 0x03
CT_PACKSQUEEZE = 0x04
CT_LZOLD = 0x05
CT_LZNEW = 0x06
CT_LZW = 0x07
CT_CRUNCH = 0x08
CT_SQUASH = 0x09
CT_COMP = 0x7f

NAME_LEN = 13
BASE_HEADER_LEN = 25      # marker .. crc
ORIGLEN_LEN = 4
RISCOS_FIELDS_LEN = 12    # load, exec, attr

# filetype &DDC (Archive) in a load address marks a directory
DIR_LOAD_MASK = 0xffffff00
DIR_LOAD_VALUE = 0xfffddc00


class SparkError(Exception):
    pass


def dos_datetime(date: int, time: int) -> Optional[datetime]:
    """Decode the MS-DOS date and time halfwords from a header, None if invalid"""
    try:
        return datetime(
            ((date >> 9) & 0x7f) + 1980, (date >> 5) & 0x0f, date & 0x1f,
            (time >> 11) & 0x1f, (time >> 5) & 0x3f, (time & 0x1f) * 2)
    except ValueError:
        return None


class SparkEntry:
    def __init__(self, path, method, date, time, crc, length, complen, data_offset,
                 load_addr=None, exec_addr=None, attr=0):
        self.path = path
        self.method = method  # with the ARCHPACK bit cleared
        self.date = date
        self.time = time
        self.crc = crc
        self.length = length
        self.complen = complen
        self.data_offset = data_offset
        # None for PC style headers with no RISC OS metadata
        self.load_addr = load_addr
        self.exec_addr = exec_addr
        self.attr = attr & 0xff

    def __repr__(self):
        return f'SparkEntry({self.path!r} method={self.method:02x} len={self.length} complen={self.complen})'


class SparkArchive(DiscImageBase):
    def __init__(self, fd: IO[bytes]):
        self.path = getattr(fd, 'name', 'archive')
        fd.seek(0, os.SEEK_SET)
        self.data = fd.read()
        self.entries: dict[PureRiscOsPath, SparkEntry] = {}
        self._parse()

    @property
    def disc_name(self):
        return os.path.basename(self.path)

    def __repr__(self):
        return 'Spark Archive ' + os.path.basename(self.path)

    @staticmethod
    def _clean_name(raw_name: bytes) -> str:
        # nspark turns control characters into NULs, ending the name there
        for i, b in enumerate(raw_name):
            if b < 0x20:
                raw_name = raw_name[:i]
                break
        # '/' is a valid RISC OS leafname char and must be preserved. ':' can't
        # appear in a name (filing-system prefix). '.' is not a valid leafname
        # char either; it only turns up in names from PC archives.
        name = raw_name.decode('iso-8859-1')
        return name.replace(':', '_').replace('.', '_')

    def _parse(self):
        data = self.data
        end = len(data)
        pos = 0
        dir_stack = []

        # like nspark, a clean EOF ends the archive even without an end marker
        while pos < end:
            if data[pos] != STARTBYTE:
                raise SparkError(f'bad archive header at offset {pos}')
            if pos + 2 > end:
                raise SparkError('truncated Spark header')
            raw_method = data[pos + 1]
            method = raw_method & ~ARCHPACK

            if method == 0:
                pos += 2
                if not dir_stack:
                    break
                dir_stack.pop()
                continue

            header_len = BASE_HEADER_LEN
            if method > CT_NOTCOMP:
                header_len += ORIGLEN_LEN
            if raw_method & ARCHPACK:
                header_len += RISCOS_FIELDS_LEN
            if pos + header_len > end:
                raise SparkError('truncated Spark header')

            raw_name, complen, date, time, crc = struct.unpack_from('<13sIHHH', data, pos + 2)
            offset = pos + BASE_HEADER_LEN
            if method > CT_NOTCOMP:
                length, = struct.unpack_from('<I', data, offset)
                offset += ORIGLEN_LEN
            else:
                length = complen
            if raw_method & ARCHPACK:
                load, exec_, attr = struct.unpack_from('<3I', data, offset)
            else:
                load = exec_ = None
                attr = 0

            name = self._clean_name(raw_name)
            if not name:
                raise SparkError(f'empty file name at offset {pos}')

            data_offset = pos + header_len

            # A stored &DDC entry is a directory. Its length covers the entries
            # inside it, which are parsed as the next entries rather than skipped.
            if method == CT_NOTCOMP2 and load is not None \
                    and load & DIR_LOAD_MASK == DIR_LOAD_VALUE:
                dir_stack.append(name)
                pos = data_offset
                continue

            pos = data_offset + complen
            path = PureRiscOsPath(*dir_stack, name)
            self.entries[path] = SparkEntry(path, method, date, time, crc, length, complen,
                                            data_offset, load, exec_, attr)

    def _file_meta(self, entry: SparkEntry) -> FileMeta:
        if entry.load_addr is None:
            # no RISC OS metadata, so make up a filetype from the DOS datestamp
            ds = dos_datetime(entry.date, entry.time) or datetime.now()
            ro_ts = int((ds - RISC_OS_EPOCH).total_seconds() * 100)
            return FileMeta(RiscOsFileMeta.from_datestamp(ro_ts), ds, entry.length)
        ro_meta = RiscOsFileMeta(entry.load_addr, entry.exec_addr, entry.attr)
        ds = ro_meta.datestamp or dos_datetime(entry.date, entry.time) or datetime.now()
        return FileMeta(ro_meta, ds, entry.length)

    def list(self) -> Generator[Tuple[PureRiscOsPath, FileMeta], None, None]:
        for path, entry in self.entries.items():
            yield path, self._file_meta(entry)

    def get_file_meta(self, path: PureRiscOsPath | str) -> FileMeta:
        return self._file_meta(self.entries[as_ro_path(path)])

    def read(self, path: PureRiscOsPath | str) -> bytes:
        entry = self.entries[as_ro_path(path)]
        comp_data = self.data[entry.data_offset:entry.data_offset + entry.complen]
        if len(comp_data) != entry.complen:
            raise SparkError(f'{path}: compressed data truncated')

        method = entry.method
        try:
            if method in (CT_NOTCOMP, CT_NOTCOMP2):
                out = comp_data
            elif method == CT_PACK:
                out = unpack_rle(comp_data)
            elif method == CT_CRUNCH:
                if comp_data:
                    out = unpack_rle(uncompress_lzw(comp_data[1:], comp_data[0]))
                else:
                    out = b''
            elif method == CT_SQUASH:
                out = uncompress_lzw(comp_data, SQUASH_BITS)
            elif method == CT_COMP:
                if comp_data:
                    out = uncompress_lzw(comp_data[1:], comp_data[0])
                else:
                    out = b''
            else:
                raise SparkError(f'{path}: unsupported compression method {method}')
        except ArcFSError as e:
            raise SparkError(f'{path}: {e}') from e

        out = out[:entry.length]
        if len(out) != entry.length:
            raise SparkError(f'{path}: expected {entry.length} bytes, got {len(out)}')
        if arc_crc16(out) != entry.crc:
            raise SparkError(f'{path}: CRC check failed')
        return out

    def open(self, path: PureRiscOsPath | str) -> Optional[IO[bytes]]:
        if as_ro_path(path) not in self.entries:
            return None
        return BytesIO(self.read(path))
