"""
PureRiscOsPath: a pathlib-based representation of RISC OS file paths.

Implements the subset of RISC OS path syntax documented in
docs/riscos_path_rules.md that is relevant to archive contents:

  '.'  directory separator
  '$'  root directory anchor ($ + '.' + path)
  '^'  parent directory component (kept literal, like '..' in pathlib)
  '/'  a valid leafname character (FileCore allows it; ISOs and ArcFS/Spark
       archives store genuine RISC OS names containing it)
  leafnames with case-insensitive comparison, case-preserving display

When a name crosses into a format that uses '/' as a path separator (ZIP
entry names, host filesystem extraction), a '/' inside a leafname is written
as '.'; reading such names back turns '.' back into '/'.

Not supported (reserved for later; constructing a path that uses them raises
ValueError): filing-system prefixes (ADFS::HardDisc4.), special fields (#...),
path variables (<Var>$Path, Name:leafname).

Uses the pathlib custom-parser hook (PurePath.parser), which requires
Python 3.13+.
"""

import posixpath
from pathlib import PurePath

# Characters invalid anywhere in a leafname (see docs/riscos_path_rules.md),
# plus control characters
INVALID_LEAFNAME_CHARS = frozenset('.:*#"') | frozenset(chr(c) for c in range(32)) | {'\x7f'}
# Characters that are only special at the start of a leafname
INVALID_LEAFNAME_START_CHARS = frozenset('$@^\\&%')

MAX_LEAFNAME_LEN = 255  # FileCore BigDir (RISC OS 4+); pre-3.8 formats were 10


class RiscOsParser:
    """
    posixpath-shaped parser describing RISC OS path syntax for pathlib.

    Only the members pathlib actually calls are meaningful; unused ones are
    provided for compatibility with the posixpath/ntpath protocol.
    """

    sep = '.'
    altsep = None
    extsep = '.'
    curdir = '@'   # currently selected directory (CSD)
    pardir = '^'   # parent directory

    @staticmethod
    def splitroot(path):
        path = str(path)
        if ':' in path:
            # ':' is only used by filing-system prefixes (ADFS::disc.),
            # special fields (#fs#field:) and path variables (fs:leaf) --
            # all deferred
            raise ValueError(f'filing-system prefixes and path variables are not supported: {path!r}')
        if path.startswith('$'):
            rel = path[1:]
            if rel.startswith('.'):
                rel = rel[1:]
            return '', '$', rel
        return '', '', path

    @staticmethod
    def splitdrive(path):
        return '', str(path)

    @staticmethod
    def isabs(path):
        return str(path).startswith('$')

    @staticmethod
    def normcase(path):
        return str(path).lower()

    @staticmethod
    def split(p):
        p = str(p)
        i = p.rfind(RiscOsParser.sep)
        if i < 0:
            return '', p
        return p[:i + 1], p[i + 1:]

    @staticmethod
    def join(path, *paths):
        result = str(path)
        for p in paths:
            p = str(p)
            if not p:
                continue
            if p.startswith('$'):
                result = p
                continue
            if result and not result.endswith(RiscOsParser.sep):
                result += RiscOsParser.sep
            result += p
        return result

    @staticmethod
    def splitext(p):
        # leafnames cannot legally contain '.', so this only matters for
        # malformed names; behave like posixpath.splitext
        return posixpath.splitext(str(p))


class PureRiscOsPath(PurePath):
    """A pure (no I/O) RISC OS path. See module docstring for syntax."""

    parser = RiscOsParser

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # pathlib 3.13 parses lazily: splitroot only runs on first access of
        # str/parts/... . Force it here so unsupported syntax (filing-system
        # prefixes, path variables) raises at construction time.
        self.parts

    @classmethod
    def _format_parsed_parts(cls, drv, root, tail):
        # pathlib joins drive + root + sep.join(tail), which assumes the root
        # includes its own trailing separator (e.g. '/'). '$' does not, so
        # insert the separator explicitly between the root and the tail.
        if drv or root:
            out = drv + root
            if tail:
                out += cls.parser.sep + cls.parser.sep.join(tail)
            return out
        return cls.parser.sep.join(tail)

    def as_zipname(self) -> str:
        """
        Return the path in the '/'-separated form used for SparkFS ZIP entry
        names and HostFS-style extraction ('!App.!RunImage' -> '!App/!RunImage').
        '/' in a leafname is encoded as '.' here, since '.' cannot appear in a
        RISC OS leafname ('!App.music/s3m' -> '!App/music.s3m'). The '$' root
        anchor is not included.
        """
        parts = self.parts
        if self.drive or self.root:
            parts = parts[1:]
        return '/'.join(p.replace('/', '.') for p in parts)

    @classmethod
    def from_zipname(cls, zipname: str) -> 'PureRiscOsPath':
        """Parse a '/'-separated SparkFS ZIP entry name into a PureRiscOsPath.

        A '.' in a zip leafname encodes a '/' that the host filesystem could
        not store directly, so each '.' becomes '/' ('!App/music.s3m' ->
        '!App.music/s3m').
        """
        return cls(*(p.replace('.', '/') for p in zipname.split('/')))

    def is_valid_leafname(self) -> bool:
        """True if the final component is a syntactically valid RISC OS leafname"""
        name = self.name
        if not name or len(name) > MAX_LEAFNAME_LEN:
            return False
        if set(name) & INVALID_LEAFNAME_CHARS:
            return False
        return name[0] not in INVALID_LEAFNAME_START_CHARS


def as_ro_path(path) -> PureRiscOsPath:
    """Coerce a str or PureRiscOsPath to PureRiscOsPath"""
    if isinstance(path, PureRiscOsPath):
        return path
    return PureRiscOsPath(path)
