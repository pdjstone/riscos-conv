from datetime import datetime
from io import BytesIO
from typing import IO

from .adfslib import ADFSdirectory, ADFSdisc, ADFSfile
from .ro_file_meta import DiscImageBase, FileMeta, RiscOsFileMeta


class RiscOsAdfsDisc(DiscImageBase):
    def __init__(self, fd):
        self.disc = ADFSdisc(fd)

    def __repr__(self):
        return f'ADFS Disc - {self.disc.disc_name}'
    
    @property
    def disc_name(self):
        return self.disc.disc_name
    
    def list(self, files=None, path=''):
        if files is None:
            files = self.disc.files
        for f in files:
            if isinstance(f, ADFSfile):
                ro_meta = RiscOsFileMeta(f.load_address, f.execution_address)
                ds = ro_meta.datestamp
                if not ds:
                    ds = datetime.now()
                # TODO: properly support/convert RISC OS paths via new pathlib type
                filename = f.name.replace('/', '.')
                full_path = (path + '/' + filename).removeprefix('/')
                yield full_path, FileMeta(ro_meta, ds, f.length)
            elif isinstance(f, ADFSdirectory):
                yield from self.list(f.files, path + '/' + f.name)
            
    def get_file_meta(self, path):
        f = self.disc.get_path(path)
        ro_meta = RiscOsFileMeta(f.load_address, f.execution_address)
        ds = ro_meta.datestamp
        if not ds:
            ds = datetime.now()
        return FileMeta(ro_meta, ds, f.length)

    def open(self, path) -> IO[bytes]:
        f = self.disc.get_path(path)
        if not f:
            return None
        return BytesIO(f.data)
    

