import io
import struct
from zipfile import ZipFile

from riscosconv.riscos_zip import zip_extra
from riscosconv.ro_file_meta import RiscOsFileMeta

# An S-format ADFS disc is 40 tracks x 16 sectors x 256 bytes.
ADF_S_SIZE = 40 * 16 * 256


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