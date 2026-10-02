import struct
from datetime import datetime

import pycdlib
import pycdlib.dr
from pycdlib import pycdlibio
from pycdlib.dr import DirectoryRecord

from .riscos_path import PureRiscOsPath, as_ro_path
from .ro_file_meta import DiscImageBase, FileMeta, RiscOsFileMeta

# https://stackoverflow.com/a/57208916
pycdlib.dr.DirectoryRecord = type(
    'DirectoryRecord',
    (pycdlib.dr.DirectoryRecord,),
    {'__slots__': ('_raw',)})

# Wrap DirectoryRecord.parse with a method that stores the raw record bytes so
# we can read the ARCHIMEDES system-use extension later.
real_dr_parse = DirectoryRecord.parse


def new_dr_parse(self, vd, record, *_):
    self._raw = record
    return real_dr_parse(self, vd, record, *_)


DirectoryRecord.parse = new_dr_parse


_ARCHIMEDES_TAG = b'ARCHIMEDES'


# http://justsolve.archiveteam.org/wiki/ARCHIMEDES_ISO_9660_extension
# A 32-byte data element in the directory record's "system use" area:
#   0-9   "ARCHIMEDES"
#   10-13 Load address  (little-endian)
#   14-17 Execution address (little-endian)
#   18-21 Attributes     (little-endian)
#   22-31 Reserved
#
# Attribute bit 0x100 indicates the original filename began with a '!'
# character (on non-Joliet discs it is stored with a leading '_' instead).
def _riscos_meta_from_raw(raw: bytes) -> RiscOsFileMeta:
    arc_offset = raw.find(_ARCHIMEDES_TAG)
    if arc_offset < 0:
        return None
    # A truncated block (corrupt record on a broken disc) counts as no
    # metadata rather than aborting the whole listing.
    if len(raw) - arc_offset < 32:
        return None
    load_addr, exec_addr, attrs = struct.unpack('<III', raw[arc_offset + 10: arc_offset + 22])
    return RiscOsFileMeta(load_addr, exec_addr, attrs)


def get_riscos_meta(self):
    return _riscos_meta_from_raw(self._raw)


DirectoryRecord.get_riscos_meta = get_riscos_meta


def is_iso9660(fd):
    offset = fd.tell()
    fd.seek(0x8001)
    magic = fd.read(5)
    fd.seek(offset)
    return magic == b'CD001'


def is_riscos_iso9660(fd) -> bool:
    if not is_iso9660(fd):
        return False
    offset = fd.tell()
    try:
        fd.seek(0)
        return _scan_for_archimedes(fd)
    finally:
        fd.seek(offset)


def _scan_for_archimedes(fd) -> bool:
    """Stream through the image looking for a well-formed ARCHIMEDES block.

    Reads in fixed-size chunks with a small overlap so a tag near a chunk
    boundary still has enough preceding bytes to locate its directory record.
    """
    CHUNK = 1 << 20      # 1 MiB
    OVERLAP = 256 + 2048  # max record length (255) + a sector, plenty of context
    tail = b''
    while True:
        chunk = fd.read(CHUNK)
        if not chunk:
            return False
        window = tail + chunk
        pos = 0
        while True:
            idx = window.find(_ARCHIMEDES_TAG, pos)
            if idx < 0:
                break
            if _archimedes_in_directory_record(window, idx):
                return True
            pos = idx + 1
        tail = window[-OVERLAP:]


def _archimedes_in_directory_record(window, pos) -> bool:
    """True if window[pos] (the 'A' of an ARCHIMEDES tag) sits inside a valid
    ISO9660 directory record whose length byte covers the whole 32-byte block.

    The enclosing record must start within 255 bytes before the tag, declare a
    length that spans past the end of the block, and place the block in its
    system-use area (i.e. after the file identifier).  This rejects the tag
    appearing in file content, padding, or a file's name.
    """
    upper = min(pos, len(window) - 33)
    for s in range(pos - 255, upper + 1):
        if s < 0:
            continue
        length = window[s]
        if length < 34 or length > 255:
            continue
        if s + length < pos + 32 or s + length > len(window):
            continue
        # The block must start at or after the end of the name field (even the
        # name's single pad byte is zero, so it can never begin the tag).
        name_len = window[s + 32]
        if s + 33 + name_len > pos:
            continue
        # A genuine block has all 10 reserved bytes zero (offset 22..31); a
        # coincidental text hit leaves prose there.  Attributes also live in
        # the low 16 bits, so the bytes straddling the load field (20..21)
        # must be zero too.
        if window[pos + 20:pos + 32] != b'\0' * 12:
            continue
        return True
    return False


class RiscOsIsoExtract:
    """Wrapper around a PyCdlibIO for a file's inode.

    PyCdlibIO only works inside a 'with' block (it sets up its internal file
    pointer in __enter__), so expose ours as a context manager too.  Used both
    directly (read()) and via 'with disc.open(name) as f:' in extract.
    """

    def __init__(self, inode, logical_block_size):
        self._inode = inode
        self._logical_block_size = logical_block_size
        self._fp = None

    def __enter__(self):
        self._fp = pycdlibio.PyCdlibIO(self._inode, self._logical_block_size)
        self._fp.__enter__()
        return self._fp

    def __exit__(self, *args):
        return self._fp.__exit__(*args)

    def read(self):
        with self:
            return self._fp.read()

    def readall(self):
        return self.read()


class _RawFileView:
    """File-like view of a file's data area for the lenient ISO reader."""

    def __init__(self, fd, offset, size):
        self._fd = fd
        self._offset = offset
        self._size = size

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        self._fd.seek(self._offset)
        return self._fd.read(self._size)

    def readall(self):
        return self.read()


class LenientIsoReader:
    """A tolerant ISO9660 walker used when pycdlib's strict parser rejects an
    ARCHIMEDES disc.  Some RISC OS CD-ROM mastering tools wrote non-conforming
    directory records or disagreed little/big-endian path tables; this reader
    navigates from the PVD root record through directory extents directly,
    ignoring path tables and skipping trailing corrupt records, so those discs
    can still be classified, listed and extracted.  Plain ISO9660 only (the
    rejected discs in the wild are not Joliet).

    Currently required by these discs in the archive:
      - Apps/P/PhotoDesk/Photodesk 3.04 (2000)(Photodesk Ltd).iso.zip
      - Apps/R/Revelation/Revelation 2.50 (1991)(Longman Logotron).iso.zip
      - Fonts/E/EFF PD Fonts Collection/EFF PD Fonts Collection 1.2
        (1997)(Electronic Font Foundary).iso.zip
    """

    SECTOR = 2048

    def __init__(self, fd):
        self._fd = fd
        pvd = self._read(16 * self.SECTOR, self.SECTOR)
        if pvd[1:6] != b'CD001':
            raise ValueError('not an ISO9660 image')
        self.disc_name = pvd[40:72].rstrip(b' ').decode('ascii', 'replace') or 'Untitled CD-ROM'
        self._root = pvd[156:190]
        self._ro_to_rec = {}

    def _read(self, offset, length):
        self._fd.seek(offset)
        return self._fd.read(length)

    def _iter_records(self, dir_bytes):
        off = 0
        while off < len(dir_bytes):
            length = dir_bytes[off]
            if length == 0 or length < 34 or off + length > len(dir_bytes):
                break
            rec = dir_bytes[off:off + length]
            off += length
            yield rec

    def _record_ident(self, rec):
        return rec[33:33 + rec[32]]

    def _record_info(self, rec):
        extent = int.from_bytes(rec[2:6], 'little')
        size = int.from_bytes(rec[10:14], 'little')
        return extent, size, rec[25]

    def _name(self, ident, ro_meta):
        name = ident.decode('ascii', 'replace')
        name = name.split(';', 1)[0]
        if name.endswith('.') and name.count('.') == 1:
            name = name[:-1]
        name = name.replace('.', '/')
        if ro_meta and (ro_meta.file_attr & 0x100) and name.startswith('_'):
            name = '!' + name[1:]
        return name

    def _dir_records(self, rec):
        extent, size, _ = self._record_info(rec)
        return self._iter_records(self._read(extent * self.SECTOR, size))

    def list(self):
        stack = [(self._root, ())]
        while stack:
            head, components = stack.pop()
            for rec in self._dir_records(head):
                flags = rec[25]
                ident = self._record_ident(rec)
                if ident in (b'\x00', b'\x01'):
                    continue
                ro_meta = _riscos_meta_from_raw(rec)
                name = self._name(ident, ro_meta)
                if not name:
                    continue
                full_ro = PureRiscOsPath(*components, name)
                self._ro_to_rec[full_ro] = rec
                if flags & 0x02:
                    stack.append((rec, components + (name,)))
                    continue
                if ro_meta is None:
                    # Files without the extension have no RISC OS metadata to
                    # attach; skip them from the RISC OS view.
                    continue
                _, size, _ = self._record_info(rec)
                ds = ro_meta.datestamp or datetime.now()
                yield full_ro, FileMeta(ro_meta, ds, size)

    def open(self, path):
        rec = self._ro_to_rec.get(as_ro_path(path))
        if rec is None:
            return None
        extent, size, _ = self._record_info(rec)
        return _RawFileView(self._fd, extent * self.SECTOR, size)


class RiscOsIsoDisc(DiscImageBase):
    """An ISO9660 CD image carrying the ARCHIMEDES RISC OS extension.

    Supports both Joliet long filenames and plain ISO9660.  Entries flagged
    with attribute bit 0x100 get a leading '!' (the disc stores them with a
    leading '_' in its system use area, Joliet just records the literal '!').

    Discs pycdlib refuses to open fall back to LenientIsoReader.
    """

    def __init__(self, fd):
        try:
            self.iso = pycdlib.PyCdlib()
            self.iso.open_fp(fd)
            self._raw = None
        except Exception:
            # Tolerate non-conforming directory records / path tables.
            self.iso = None
            self._raw = LenientIsoReader(fd)
        # Prefer the Joliet tree when present: it carries true mixed-case RISC
        # OS names (including leading '!' characters), otherwise fall back to
        # the plain ISO9660 tree.
        self.use_joliet = self._raw is None and self.iso.has_joliet()
        # Map of RISC OS path -> DirectoryRecord, populated while walking so
        # extraction can locate the exact record's inode.
        self._ro_to_iso = {}
        self._indexed = False

    def __repr__(self):
        return f'ISO CD-ROM - {self.disc_name}'

    @property
    def disc_name(self):
        if self._raw is not None:
            return self._raw.disc_name
        name = self.iso.pvd.volume_identifier.rstrip(b' ').decode('ascii', 'replace')
        return name or 'Untitled CD-ROM'

    def _rec_name(self, record: DirectoryRecord) -> (str, bool):
        """Return (riscos_name, bang_prefix) for a directory record."""
        ident = record.file_ident
        ro_meta = record.get_riscos_meta()
        if self.use_joliet:
            # Joliet stores names as UTF-16 (big-endian in the record space,
            # pycdlib hands back the raw utf-16be bytes in file_ident).
            try:
                name = ident.decode('utf-16-be')
            except UnicodeDecodeError:
                name = ident.decode('latin-1', 'replace')
           
            name = name.replace('.', '/')
            # Joliet names may still carry the '!' for this extension; ensure we
            # restore it if the metadata flags it and the name has a '_'.
            if ro_meta and (ro_meta.file_attr & 0x100) and name.startswith('_') and not name.startswith('!'):
                name = '!' + name[1:]
            return name, ro_meta is not None and bool(ro_meta.file_attr & 0x100)
        # Plain ISO9660: ASCII, uppercase, 8.3, with ';VER' version suffix.
        name = ident.decode('ascii', 'replace')
        name = name.split(';', 1)[0]
        # An 8.3 name with no extension is padded to "NAME.;1"; the split
        # above leaves a dangling '.' with no real extension behind.
        if name.endswith('.') and name.count('.') == 1:
            name = name[:-1]

        name = name.replace('.', '/')
        if ro_meta and (ro_meta.file_attr & 0x100) and name.startswith('_'):
            name = '!' + name[1:]
        return name, bool(ro_meta and (ro_meta.file_attr & 0x100))

    def list(self):
        if self._raw is not None:
            yield from self._raw.list()
            self._indexed = True
            return
        seen = set()
        root = self.iso.joliet_vd.root_directory_record() if self.use_joliet \
            else self.iso.pvd.root_directory_record()
        yield from self._walk_dir(root, PureRiscOsPath(''), seen)
        self._indexed = True

    def _walk_dir(self, dir_record, ro_prefix, seen):
        if id(dir_record) in seen:
            return
        seen.add(id(dir_record))

        for record in dir_record.children:
            if record.file_ident in (b'\x00', b'\x01'):
                # "." and ".." entries.
                continue
            name, bang = self._rec_name(record)
            if not name:
                continue
            full_ro = ro_prefix / name
            if record.is_dir():
                self._ro_to_iso[full_ro] = record
                yield from self._walk_dir(record, full_ro, seen)
                continue
            ro_meta = record.get_riscos_meta()
            if ro_meta is None:
                # Files without the extension have no RISC OS metadata to
                # attach; skip them from the RISC OS view.
                continue
            self._ro_to_iso[full_ro] = record
            ds = ro_meta.datestamp or datetime.now()
            yield full_ro, FileMeta(ro_meta, ds, record.data_length)

    def get_file_meta(self, path):
        path = as_ro_path(path)
        for name, meta in self.list():
            if name == path:
                return meta
        return None

    def open(self, path):
        path = as_ro_path(path)
        if not self._indexed:
            list(self.list())
        if self._raw is not None:
            return self._raw.open(path)
        record = self._ro_to_iso.get(path)
        if record is None:
            return None
        return RiscOsIsoExtract(record.inode, self.iso.logical_block_size)

