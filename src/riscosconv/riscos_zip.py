import os
import shutil
import struct
import tempfile
from datetime import datetime
from pathlib import Path
from typing import IO, Optional
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo
from .ro_file_meta import (
    DiscImageBase, FileMeta, RiscOsFileMeta, unix_timestamp_to_ro_timestamp,
)
from .riscos_path import PureRiscOsPath, as_ro_path
from .zip_implode import setup_zip_implode

ZIP_EXT_ACORN = 0x4341    # 'AC' - SparkFS / Acorn
ZIP_ID_ARC0 = 0x30435241  # 'ARC0'


class RiscOsZip(DiscImageBase):
    def __init__(self, fd):
        self.zf = ZipFile(fd)
        # Key entries by parsed RISC OS path. Dots in a zip leafname (from
        # foreign zippers) parse as directory separators, matching RISC OS
        # semantics where '.' cannot appear in a leafname. Lookups must go
        # through this dict, not back through as_zipname(), or such names
        # would not round-trip.
        self._entries = {}
        for info in self.zf.infolist():
            if info.is_dir():
                continue
            # ':' cannot appear in a RISC OS name (it starts a filing-system
            # prefix or path variable); substitute so one foreign entry name
            # can't abort parsing of the whole archive
            name = info.filename.replace(':', '_')
            self._entries.setdefault(PureRiscOsPath.from_zipname(name), info)

    @property
    def disc_name(self):
        return self.zf.filename

    def list(self):
        for path, info in self._entries.items():
            ro_meta = info.getRiscOsMeta()
            if ro_meta is None:
                # no Acorn extra field (foreign zipper): fall back to text
                # filetype with the zip's own timestamp
                ro_meta = RiscOsFileMeta.from_datestamp(
                    unix_timestamp_to_ro_timestamp(datetime(*info.date_time).timestamp()))
            m = FileMeta(ro_meta, ro_meta.datestamp, info.file_size)
            if not ro_meta.datestamp:
                m.timestamp = datetime(*info.date_time)
            yield path, m

    def get_file_meta(self, name) -> FileMeta:
        info = self._entries[as_ro_path(name)]
        ro_meta = info.getRiscOsMeta()
        if ro_meta is None:
            ro_meta = RiscOsFileMeta.from_datestamp(
                unix_timestamp_to_ro_timestamp(datetime(*info.date_time).timestamp()))
        return FileMeta(ro_meta, ro_meta.datestamp, info.file_size)

    def open(self, name) -> Optional[IO[bytes]]:
        info = self._entries.get(as_ro_path(name))
        if info is None:
            return None
        return self.zf.open(info)

def parse_riscos_zip_ext(buf: bytes, offset, fieldLen):
    # See https://www.davidpilling.com/wiki/index.php/SparkFS "A Comment on Zip files"
    if fieldLen == 24:
        fieldLen = 20
    id2 = int.from_bytes(buf[offset+4:offset+8], 'little')
    if id2 != ZIP_ID_ARC0:
        return None

    load, exec, attr = struct.unpack('<III', buf[offset+8:offset+8+12])
    meta = RiscOsFileMeta(load, exec, attr)
    return meta, fieldLen


if not hasattr(ZipInfo, '_decodeExtra'):
    raise Exception("Cannot monkey patch ZipInfo - has implementation changed?")

def _decodeExtra(self, *args):
    pass


def zip_extra(ro_meta: RiscOsFileMeta) -> bytes:
    return struct.pack('<HHIIII', ZIP_EXT_ACORN, 20, ZIP_ID_ARC0, 
        ro_meta.load_addr, ro_meta.exec_addr, ro_meta.file_attr)
    
# prevent Python from trying to decode extra ZIP fields
ZipInfo._decodeExtra = _decodeExtra

def _decodeRiscOsExtra(self):
        offset = 0
        
        # extraFieldTotalLength is total length of all extra fields
        # Iterate through each extra field and parse if known
        while offset < len(self.extra):
            fieldType, fieldLen = struct.unpack('<HH', self.extra[offset:offset+4])
            extraMeta = None
            overrideFieldLen = None
            if fieldType == ZIP_EXT_ACORN:
                extraMeta, overrideFieldLen = parse_riscos_zip_ext(self.extra, offset, fieldLen)
                return extraMeta
            if overrideFieldLen and overrideFieldLen > 0:
                offset += overrideFieldLen + 4; 
            else:
                offset += fieldLen + 4
                
        return None

ZipInfo.getRiscOsMeta = _decodeRiscOsExtra

# We need to override the default filename codec 
# Python >= 3.11 supports metadata_encoding param but only for reading
import encodings.cp437
import encodings.iso8859_1


def _encodeFilenameFlags(self):
    return self.filename.encode('iso-8859-1'), self.flag_bits


ZipInfo._encodeFilenameFlags = _encodeFilenameFlags
encodings.cp437.decoding_table = encodings.iso8859_1.decoding_table


def get_riscos_zipinfo(path: Path, base_path: Path):
    meta = RiscOsFileMeta.from_filepath(path)
    if ',' in path.stem:
        zip_path, _ = str(path.relative_to(base_path)).rsplit(',', 1)
    else:
        zip_path = str(path.relative_to(base_path)).removesuffix(path.suffix)
    ds = meta.datestamp
    if not ds:
        ds = datetime.fromtimestamp(path.stat().st_mtime)
   
    date_time = ds.year, ds.month, ds.day, ds.hour, ds.minute, ds.second
    zipinfo = ZipInfo(zip_path, date_time)
    zipinfo.extra = zip_extra(meta)
    st = os.stat(path)
    if st.st_size > 512:
        zipinfo.compress_type = ZIP_DEFLATED
    return zipinfo


def zip_extract_ro_path(zipfile: ZipFile, path: Path, filetype=None):
    for info in zipfile.infolist():
        if info.is_dir():
            continue
        ro_meta = info.getRiscOsMeta()
        if not ro_meta:
            continue
        if info.filename.lower() != str(path).lower():
            continue
        if filetype and ro_meta.filetype != filetype:
            continue
        return zipfile.open(info)

def convert_disc_to_zip(disc: DiscImageBase, zip_path, extract_paths: list[str] | None = None):
    assert type(extract_paths) is list

    zf = ZipFile(zip_path, 'w')
    for path, file_meta in disc.list():
        path = as_ro_path(path)
        skip_file = False
        if extract_paths:
            skip_file = True
            for p in extract_paths:
                # component-wise prefix match, so 'Draw' doesn't match 'DrawDemo'
                p_parts = as_ro_path(p).parts
                parts = path.parts
                if len(parts) >= len(p_parts) and tuple(
                        part.lower() for part in parts[:len(p_parts)]
                ) == tuple(part.lower() for part in p_parts):
                    skip_file = False
                    break
        if skip_file:
            continue

        ds = file_meta.timestamp

        date_time = ds.year, ds.month, ds.day, ds.hour, ds.minute, ds.second
        zipinfo = ZipInfo(path.as_zipname(), date_time)
        zipinfo.extra = zip_extra(file_meta.ro_meta)
        if file_meta.file_size > 512:
            zipinfo.compress_type = ZIP_DEFLATED
        print(path, file_meta.ro_meta)
        with disc.open(path) as fd:
            data = fd.read()
        zf.writestr(zipinfo, data, compresslevel=9)
    zf.close()

def zip_member_to_tempfile(zf: ZipFile, info) -> IO[bytes]:
    """Materialise a zip member to a real temp file.

    PyCdlib seeks around an image heavily; a zipfile.ZipExtFile re-decompresses
    from the start on every seek, so working on zipped ISO members directly is
    O(n^2) and painfully slow.  Write the member to disk once and read it back
    as a real file.  A TemporaryFile is unlinked at creation (POSIX) and its
    space freed as soon as the handle is closed or garbage-collected, so many
    loads in one process cannot accumulate temp files.
    """
    tmp = tempfile.TemporaryFile()
    with zf.open(info, 'r') as src:
        shutil.copyfileobj(src, tmp)
    tmp.seek(0)
    return tmp