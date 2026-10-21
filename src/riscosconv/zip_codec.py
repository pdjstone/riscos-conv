"""
Monkey-patching of zipfile for ZIP methods Python does not ship with.

The shrink and implode algorithm modules (zip_shrink.py, zip_implode.py)
register their decompressor here at import time; setup_zip_codecs() installs
one set of zipfile patches that dispatch on the method number.  This lives
apart from the algorithms so a single patch covers both methods.
"""

import zipfile

_ZIP_METHODS = {}  # method number -> decompressor factory


def register(method, method_attr, decompressor_cls):
    """Register a decompressor for a zip method and expose the method on zipfile."""
    setattr(zipfile, method_attr, method)
    _ZIP_METHODS[method] = decompressor_cls


def setup_zip_codecs():
    """Monkey-patch zipfile to use the registered decompressors."""
    _orig_check = zipfile._check_compression

    def _check(compression):
        if compression in _ZIP_METHODS:
            return
        return _orig_check(compression)

    zipfile._check_compression = _check

    _orig_get_decomp = zipfile._get_decompressor

    def _get_decomp(compress_type):
        if compress_type in _ZIP_METHODS:
            return _ZIP_METHODS[compress_type]()
        return _orig_get_decomp(compress_type)

    zipfile._get_decompressor = _get_decomp

    # The decompressors buffer the whole member, so they need the member's
    # flag_bits, compress_size and file_size, which _get_decompressor does not
    # pass. Attach them after ZipExtFile is constructed (and again on seek,
    # which rebuilds the decompressor).
    _orig_ext_init = zipfile.ZipExtFile.__init__

    def _ext_init(self, fileobj, mode, zipinfo, pwd=None, close_fileobj=False):
        _orig_ext_init(self, fileobj, mode, zipinfo, pwd=pwd,
                       close_fileobj=close_fileobj)
        if self._compress_type in _ZIP_METHODS:
            self._codec_flag_bits = zipinfo.flag_bits
            self._decompressor.set_info(zipinfo.flag_bits,
                                        zipinfo.compress_size,
                                        zipinfo.file_size)

    zipfile.ZipExtFile.__init__ = _ext_init

    _orig_ext_seek = zipfile.ZipExtFile.seek

    def _ext_seek(self, offset, whence=0):
        result = _orig_ext_seek(self, offset, whence)
        if self._compress_type in _ZIP_METHODS:
            # seek() recreates the decompressor; re-attach the member info.
            self._decompressor.set_info(getattr(self, '_codec_flag_bits', 0),
                                        self._orig_compress_size,
                                        self._orig_file_size)
        return result

    zipfile.ZipExtFile.seek = _ext_seek


setup_zip_codecs()
