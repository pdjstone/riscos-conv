import io
import struct
from zipfile import ZipFile

import pycdlib
import pycdlib.dr as drmod

from riscosconv.riscos_zip import zip_extra
from riscosconv.ro_file_meta import RiscOsFileMeta

# An S-format ADFS disc is 40 tracks x 16 sectors x 256 bytes.
ADF_S_SIZE = 40 * 16 * 256


# -- Joliet test-image writer -------------------------------------------------
# A RISC OS disc mastered for both trees stores the RISC OS name in the plain
# ISO9660 tree (type separator '/' as in 'COVER/JPG') but Joliet forbids '/',
# so the same file appears as 'Cover.jpg'.  pycdlib cannot put a '/' in an ISO9660
# identifier, so a plain tree with '/' names cannot be produced by pycdlib; the
# test helper instead masters only the Joliet view (which is what RiscOsIsoDisc
# reads when use_joliet is set) and injects the ARCHIMEDES system-use block on
# write, so the listed RISC OS paths come from Joliet names.
def _install_joliet_meta() -> None:
    """Extend the write path so a record tagged ._ro_meta gets an ARCHIMEDES
    system-use block spliced in after its identifier."""
    base = drmod.DirectoryRecord
    if getattr(base, '_joliet_meta_slot', False):
        return
    drmod.DirectoryRecord = type(
        'DirectoryRecord',
        (base,),
        {'__slots__': ('_ro_meta',), '_joliet_meta_slot': True})
    _orig_record = base.record

    def record_with_arch(self):
        out = _orig_record(self)
        meta = getattr(self, '_ro_meta', None)
        if meta is None:
            return out
        pad = (self._FMT_SIZE + self.len_fi) % 2
        insert_at = self._FMT_SIZE + self.len_fi + pad
        out = bytearray(out)
        out[insert_at:insert_at] = meta
        out[0] += len(meta)
        return bytes(out)

    base.record = record_with_arch


def build_joliet_iso(entries) -> bytes:
    """Master a Joliet ISO whose files carry ARCHIMEDES metadata.

    ``entries`` is a list of ``(name, data, load, exec, attrs)`` where ``name``
    is the '/' separator-separated Joliet path stored on disc (both the disc
    root and nested directories are supported), e.g. ``'Cover.jpg'`` for a root
    leaf whose RISC OS name is ``'Cover/jpg'`` or ``'AITD/Docs/Cover.jpg'`` for
    the same leaf under two directories.  The plain ISO9660 tree exists
    (pycdlib's 8.3 translation of the name, with '!' mapped to '_') but carries
    no ARCHIMEDES block, mirroring discs where only the Joliet view is usable.
    """
    _install_joliet_meta()
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=3)
    made_dirs = set()
    for name, data, load, exec_, attrs in entries:
        if isinstance(data, str):
            data = data.encode()
        # pycdlib cannot create intermediate directories implicitly, so create
        # each distinct parent chain on both trees first.
        components = name.split('/')
        for depth in range(1, len(components)):
            parent = '/'.join(components[:depth])
            if parent in made_dirs:
                continue
            made_dirs.add(parent)
            iso.add_directory(iso_path='/' + '/'.join(_iso83(c) for c in components[:depth]),
                              joliet_path=f'/{parent}')
        iso_path = '/' + '/'.join(
            _iso83(p) for p in components) + ';1'
        iso.add_fp(io.BytesIO(data), len(data), iso_path=iso_path,
                   joliet_path=f'/{name}')
        rec = _find_joliet_path(iso.joliet_vd.root_directory_record(), name)
        rec._ro_meta = b'ARCHIMEDES' + struct.pack('<III', load, exec_, attrs) + b'\0' * 10
    out = io.BytesIO()
    iso._write_fp(out, 8192, None, None)
    return out.getvalue()


def _iso83(name):
    # Plain-tree identifier: 8.3, uppercase, dots/slashes/bangs only in the
    # forms pycdlib accepts ('!' cannot appear in an ISO9660 filename, so a
    # disc stores a leading '!' as '_' in the plain tree).
    return name.replace('.', '_').replace('!', '_').upper()[:8]


def _find_joliet_path(root, path):
    rec = root
    for comp in path.split('/'):
        rec = _find_joliet_child(rec, comp)
    return rec


def _find_joliet_child(rec, name):
    for child in rec.children:
        if child.file_ident in (b'\x00', b'\x01'):
            continue
        try:
            if child.file_ident.decode('utf-16-be') == name:
                return child
        except UnicodeDecodeError:
            pass
    raise KeyError(f'no Joliet child {name!r}')


def build_adf(entries=None) -> bytes:
    """Build a minimal valid S-format ADFS disc image.

    The old-format catalogue lives in sector 2 (offset 512): a "Hugo" root
    marker followed by file entries, an end-of-catalogue terminator, and a
    directory tail carrying the disc title. The tail triggers the disc_name
    parsing path (parent == head), which previously fed an already-decoded
    string back into _safe and crashed on spaces.

    ``entries`` is an optional list of ``(name_bytes, load, exe, length,
    sector, atts)`` tuples; when omitted a single plain file entry named
    'Test' is created. Names are 10 bytes on disc; attribute bits are stored
    in the top bit of each name character (old-format ADFS).
    """
    img = bytearray(ADF_S_SIZE)
    img[512] = 1                       # dir_seq
    img[513:517] = b'Hugo'             # root directory marker
    if entries is None:
        entries = [(b'Test', 0, 0, 1, 100, 0)]
    p = 517
    for name, load, exe, length, sector, atts in entries:
        assert len(name) <= 10
        img[p:p + 10] = name.ljust(10, b'\0')
        img[p + 10: p + 14] = load.to_bytes(4, 'little')
        img[p + 14: p + 18] = exe.to_bytes(4, 'little')
        img[p + 18: p + 22] = length.to_bytes(4, 'little')
        img[p + 22: p + 25] = sector.to_bytes(3, 'little')
        img[p + 25] = atts
        p += 26
    img[p] = 0                        # end of catalogue
    tail = 512 + 4 * 256
    img[tail + 256 - 5: tail + 256 - 1] = b'Hugo'                # dir_end marker
    img[tail + 256 - 52: tail + 256 - 42] = b'Root' + b'\0' * 6  # dir_name
    img[tail + 256 - 42: tail + 256 - 39] = (2).to_bytes(3, 'little')  # parent
    img[tail + 256 - 39: tail + 256 - 20] = b'Test Disc' + b'\0' * 10   # title
    img[tail + 256 - 6] = 1           # endseq == dir_seq
    return bytes(img)


def build_hfe() -> bytes:
    """Build a minimal HFE disc image (magic + footer byte)."""
    return b'HXCPICFE' + b'\x00' * 59 + b'\xff'


def both16(n):
    return struct.pack('<H', n) + struct.pack('>H', n)


def both32(n):
    return struct.pack('<I', n) + struct.pack('>I', n)


def sysuse_block(load, exec_, attrs):
    # ARCHIMEDES ISO 9660 extension: 0-9 tag, 10-13 load, 14-17 exec,
    # 18-21 attrs (bit 0x100 = original name began with '!'), 22-31 reserved.
    return b'ARCHIMEDES' + struct.pack('<III', load, exec_, attrs) + b'\0' * 10


def dir_record(extent, size, ident, flags=0, sysuse=b''):
    """A minimal ISO9660 directory record (34 bytes + ident + system use)."""
    ident = ident if isinstance(ident, bytes) else ident.encode()
    len_fi = len(ident)
    pad = 1 if len_fi % 2 == 0 else 0
    length = 33 + len_fi + pad + len(sysuse)
    if length % 2:
        length += 1
    rec = bytearray(length)
    rec[0] = length
    rec[1] = 0
    rec[2:10] = both32(extent)
    rec[10:18] = both32(size)
    rec[18:25] = b'\0' * 7
    rec[25] = flags
    rec[28:32] = both16(1)
    rec[32] = len_fi
    rec[33:33 + len_fi] = ident
    if pad:
        rec[33 + len_fi] = 0
    if sysuse:
        su_off = 33 + len_fi + pad
        rec[su_off:su_off + len(sysuse)] = sysuse
    return bytes(rec)


def build_iso(entries, joliet=False) -> bytes:
    """Build a minimal single-level ISO9660 image with ARCHIMEDES metadata.

    ``entries`` is a list of ``(ident, data, load, exec, attrs)`` tuples where
    ``ident`` is the raw identifier stored in the directory record (e.g.
    ``b'_Boot'`` with attrs bit 0x100 for a RISC OS '!Boot').
    """
    LBS = 2048
    PVD = 16
    TERM = 17
    PATH_L = 18
    PATH_M = 19
    ROOT = 20
    data_sector = 21
    img = bytearray(LBS * 512)

    # PVD (ISO 9660 8.4 layout)
    img[PVD * LBS] = 1
    img[PVD * LBS + 1: PVD * LBS + 6] = b'CD001'
    img[PVD * LBS + 6] = 1
    img[PVD * LBS + 80: PVD * LBS + 88] = both32(512)          # volume space size
    img[PVD * LBS + 120: PVD * LBS + 128] = both16(1) * 2      # set size + seqnum
    img[PVD * LBS + 128: PVD * LBS + 132] = both16(LBS)        # logical block size
    img[PVD * LBS + 132: PVD * LBS + 140] = both32(10)         # path table size
    img[PVD * LBS + 140: PVD * LBS + 144] = struct.pack('<I', PATH_L)
    img[PVD * LBS + 148: PVD * LBS + 152] = struct.pack('>I', PATH_M)
    img[PVD * LBS + 156: PVD * LBS + 190] = dir_record(ROOT, LBS, b'\0')
    img[PVD * LBS + 40: PVD * LBS + 72] = b'TEST'.ljust(32)    # volume id

    # Terminator
    img[TERM * LBS] = 255
    img[TERM * LBS + 1: TERM * LBS + 6] = b'CD001'
    img[TERM * LBS + 6] = 1

    # Path tables: one root record, L is little-endian, M is big-endian.
    pt_l = b'\x01\x00' + struct.pack('<I', ROOT) + struct.pack('<H', 1) + b'\0' * 2
    pt_m = b'\x01\x00' + struct.pack('>I', ROOT) + struct.pack('>H', 1) + b'\0' * 2
    img[PATH_L * LBS: PATH_L * LBS + 10] = pt_l
    img[PATH_M * LBS: PATH_M * LBS + 10] = pt_m

    # Root directory: '.', '..', then one record per entry.
    root_recs = [dir_record(ROOT, LBS, b'\0'), dir_record(ROOT, LBS, b'\x01')]
    sector = data_sector
    for ident, data, load, exec_, attrs in entries:
        if isinstance(data, str):
            data = data.encode()
        img[sector * LBS: sector * LBS + len(data)] = data
        root_recs.append(dir_record(sector, len(data), ident,
                                    sysuse=sysuse_block(load, exec_, attrs)))
        sector += 1
    img[ROOT * LBS: ROOT * LBS + LBS] = b''.join(root_recs).ljust(LBS, b'\0')

    return bytes(img)


def ro_load(filetype):
    return (0xfff << 20) | (filetype << 8)


def make_zip(members):
    """Create an in-memory zip from {name: data}."""
    buf = io.BytesIO()
    with ZipFile(buf, 'w') as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def ro_zip_bytes():
    """A plain file with RISC OS metadata but no disc image inside."""
    meta = RiscOsFileMeta(0xfff00123, 0x8000)
    buf = io.BytesIO()
    with ZipFile(buf, 'w') as zf:
        info = __import__('zipfile').ZipInfo('readme')
        info.extra = zip_extra(meta)
        zf.writestr(info, b'hello')
    return buf.getvalue()