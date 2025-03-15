from dataclasses import dataclass
from datetime import datetime, timedelta
import os
from pathlib import Path
import re
from typing import IO, Generator, Protocol, Tuple


RISC_OS_EPOCH = datetime(1900,1,1,0,0,0)

RISC_OS_COMMA_FILETYPE_PATTERN = r',([a-f0-9]{3})$'
RISC_OS_LOAD_EXEC_PATTERN = r',([a-f0-9]{8})-([a-f0-9]{8})$'

RO_ZIP = 0xa91
RO_TEXT = 0xfff
RO_DATA = 0xffd

FILE_EXT_MAP = {
    '': RO_TEXT,
    '.txt': RO_TEXT,
    '.zip': RO_ZIP
}

RISC_OS_ARCHIVE_TYPES = (RO_ZIP, )
DEFAULT_RO_FILETYPE = 0xfff


# See http://www.riscos.com/support/developers/prm/fileswitch.html#idx-3804
def make_load_exec(filetype, ro_timestamp):
    load_addr = (0xfff << 20) | (filetype << 8) | (ro_timestamp >> 32)
    exec_addr = ro_timestamp & 0xffffffff
    return load_addr, exec_addr


def unix_timestamp_to_ro_timestamp(timestamp):
    delta = datetime.fromtimestamp(timestamp) - RISC_OS_EPOCH
    centiseconds = int(delta.total_seconds() * 100)
    return centiseconds


class RiscOsFileMeta:
    def __init__(self, load_addr, exec_addr, attr=3):
        self.load_addr = load_addr
        self.exec_addr = exec_addr
        self.file_attr = attr

    @property
    def filetype(self):
        if self.load_addr >> 20 == 0xfff:
            return self.load_addr >> 8 & 0xfff
        return None

    @property
    def datestamp(self):
        if self.load_addr >> 20 == 0xfff:
            cs = ((self.load_addr & 0xff) << 32) | self.exec_addr
            delta = timedelta(milliseconds=cs*10)
            return RISC_OS_EPOCH + delta
        return None

    def hostfs_file_ext(self):
        if self.load_addr >> 20 == 0xfff:
            return f',{self.filetype:03x}'
        return f',{self.load_addr:08x}-{self.exec_addr:08x}'

    def __repr__(self):
        if self.filetype:
            return f'RiscOsFileMeta(type={self.filetype:03x} date={self.datestamp} attr={self.file_attr:x})'
        else:   
            return f'RiscOsFileMeta(load={self.load_addr:x} exec={self.exec_addr:x} attr={self.file_attr:x})'

    @staticmethod
    def from_filepath(path: Path):
        leaf_name = path.name
        st = os.stat(path)
        ts = unix_timestamp_to_ro_timestamp(st.st_mtime)
        if m := re.search(RISC_OS_COMMA_FILETYPE_PATTERN, leaf_name, re.IGNORECASE):
            filetype = int(m.group(1), 16)
            load_addr, exec_addr = make_load_exec(filetype, ts)
        elif m := re.search(RISC_OS_LOAD_EXEC_PATTERN, leaf_name, re.IGNORECASE):
            load_addr = int(m.group(1), 16)
            exec_addr = int(m.group(2), 16)
        else:
            extension = path.suffix
            filetype = FILE_EXT_MAP.get(extension.lower(), None)
            if not filetype:
                raise Exception(f"No RISC OS filetype for {leaf_name} {extension}")
            load_addr, exec_addr = make_load_exec(filetype, ts)
        return RiscOsFileMeta(load_addr, exec_addr)



@dataclass
class FileMeta:
    ro_meta: RiscOsFileMeta
    timestamp: datetime
    file_size: int



class DiscImageBase(Protocol):

    @property
    def disc_name(self) -> str:
        pass

    def list(self) -> Generator[Tuple[str, FileMeta], None, None]:
        pass
    
    def get_file_meta(self, path: str) -> FileMeta:
        pass

    def open(self, path: str) -> IO[bytes]:
        pass
