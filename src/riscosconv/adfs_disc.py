from datetime import datetime
from io import BytesIO
from typing import IO

from .adfslib import ADFSdirectory, ADFSdisc, ADFSfile
from .riscos_path import PureRiscOsPath, as_ro_path
from .ro_file_meta import DiscImageBase, FileMeta, RiscOsFileMeta


class RiscOsAdfsDisc(DiscImageBase):
    def __init__(self, fd):
        self.disc = ADFSdisc(fd)

    def __repr__(self):
        return f'ADFS Disc - {self.disc.disc_name}'

    @property
    def disc_name(self):
        return self.disc.disc_name

    def list(self, files=None, components=()):
        if files is None:
            files = self.disc.files
        for f in files:
            if isinstance(f, ADFSfile):
                ro_meta = RiscOsFileMeta(f.load_address, f.execution_address)
                ds = ro_meta.datestamp
                if not ds:
                    ds = datetime.now()
                # '/' is a valid RISC OS leafname char: the on-disc name
                # (e.g. a long-filename 'NAME/NNN') is one component, not a
                # path separator here, so pass it through unchanged.
                yield PureRiscOsPath(*components, f.name), FileMeta(ro_meta, ds, f.length)
            elif isinstance(f, ADFSdirectory):
                yield from self.list(f.files, components + (f.name,))

    def _find(self, files, components):
        if not components:
            return None
        head, *rest = components
        for f in files:
            if f.name.lower() != head.lower():
                continue
            if not rest:
                return f
            if isinstance(f, ADFSdirectory):
                found = self._find(f.files, rest)
                if found is not None:
                    return found
        return None

    def get_file_meta(self, path):
        f = self._find(self.disc.files, as_ro_path(path).parts)
        if f is None or not isinstance(f, ADFSfile):
            return None
        ro_meta = RiscOsFileMeta(f.load_address, f.execution_address)
        ds = ro_meta.datestamp
        if not ds:
            ds = datetime.now()
        return FileMeta(ro_meta, ds, f.length)

    def open(self, path) -> IO[bytes]:
        f = self._find(self.disc.files, as_ro_path(path).parts)
        if f is None or not isinstance(f, ADFSfile):
            return None
        return BytesIO(f.data)