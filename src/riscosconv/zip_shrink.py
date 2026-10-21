import zipfile

from .zip_codec import register

# ZIP compression method 1 (Shrink). Python's zipfile raises
# NotImplementedError for method 1, so the decompressor is registered with
# zip_codec, which monkey-patches zipfile to use it. The algorithm is a port
# of Info-ZIP's unshrink.c (v1.22): dynamic LZW with code sizes 9-13 bits,
# explicit code-size signalling (escape 256,1) and partial clearing of leaf
# nodes (escape 256,2).
ZIP_SHRUNK = 1

HSIZE = 8192             # 2^13: maximum code value plus one
BOGUSCODE = 256          # the escape code (control signalling)
FREE_CODE = HSIZE        # marker: dictionary slot is unused
HAS_CHILD = HSIZE << 1   # flag bit: node has a child (keep it on partial clear)
CODE_MASK = HSIZE - 1    # lower 13 bits of parent[] hold the parent's index


class _Bits:
    """Continuous LSB-first bit reader (mirrors READBITS in unzpriv.h)."""

    def __init__(self, data):
        self.data = data
        self.pos = 0
        self.bitbuf = 0
        self.bits_left = 0
        self.eof = False

    def read(self, n):
        if n > self.bits_left:
            self.eof = True
            while self.bits_left <= 24 and self.pos < len(self.data):
                self.bitbuf |= self.data[self.pos] << self.bits_left
                self.pos += 1
                self.bits_left += 8
                self.eof = False
        v = self.bitbuf & ((1 << n) - 1)
        self.bitbuf >>= n
        self.bits_left -= n
        return v


def _partial_clear(parent, lastcodeused):
    """Partial clear: free all leaf nodes up to lastcodeused (unshrink.c)."""
    for code in range(BOGUSCODE + 1, lastcodeused + 1):
        cparent = parent[code] & CODE_MASK
        if cparent > BOGUSCODE:
            parent[cparent] |= HAS_CHILD
    for code in range(BOGUSCODE + 1, lastcodeused + 1):
        if parent[code] & HAS_CHILD:
            parent[code] &= ~HAS_CHILD
        else:
            parent[code] = FREE_CODE


def unshrink(src: bytes) -> bytes:
    """Decompress a Shrunk (ZIP method 1) member into its payload bytes."""
    parent = [0] * HSIZE
    value = [0] * HSIZE
    for code in range(256):
        value[code] = code
        parent[code] = BOGUSCODE
    for code in range(BOGUSCODE + 1, HSIZE):
        parent[code] = FREE_CODE

    bits = _Bits(src)
    codesize = 9
    out = bytearray()

    oldcode = bits.read(codesize)
    if bits.eof:
        return bytes(out)
    finalval = oldcode
    out.append(finalval)
    lastfreecode = BOGUSCODE

    while True:
        code = bits.read(codesize)
        if bits.eof:
            break
        if code == BOGUSCODE:
            code = bits.read(codesize)
            if bits.eof:
                break
            if code == 1:
                codesize += 1
                if codesize > 13:
                    raise zipfile.BadZipFile("shrink: codesize > 13")
            elif code == 2:
                _partial_clear(parent, lastfreecode)
                lastfreecode = BOGUSCODE
            continue

        curcode = code
        stack = bytearray()
        if parent[code] == FREE_CODE:
            stack.append(finalval)
            code = oldcode
        while code != BOGUSCODE:
            if parent[code] == FREE_CODE:
                stack.append(finalval)
                code = oldcode
            else:
                stack.append(value[code])
                code = parent[code] & CODE_MASK
        stack.reverse()
        finalval = stack[0]
        out += stack

        code = lastfreecode + 1
        while code < HSIZE and parent[code] != FREE_CODE:
            code += 1
        lastfreecode = code
        if code >= HSIZE:
            raise zipfile.BadZipFile("shrink: code overflow")
        value[code] = finalval
        parent[code] = oldcode
        oldcode = curcode

    return bytes(out)


class _ShrunkDecompressor:
    """zipfile-compatible decompressor for method 1.

    Buffers the whole compressed member (shrink needs all input to decode),
    then emits the decompressed data in one go.
    """

    def __init__(self):
        self.eof = False
        self._buf = bytearray()
        self._compress_size = None
        self._file_size = None
        self._out = b''

    def set_info(self, flag_bits, compress_size, file_size):
        # flag_bits is unused by shrink; kept for a set_info signature common
        # with the implode decompressor.
        self._compress_size = compress_size
        self._file_size = file_size

    def decompress(self, data):
        if self.eof:
            return b''
        self._buf += data
        if self._compress_size is None:
            # Info not attached yet (should not happen via the patched init).
            return b''
        if self._compress_size == 0:
            self.eof = True
            return b''
        if len(self._buf) < self._compress_size:
            return b''
        out = unshrink(bytes(self._buf[:self._compress_size]))
        if len(out) != self._file_size:
            raise zipfile.BadZipFile(
                f"shrink: decoded {len(out)} bytes, expected {self._file_size}")
        self._out = out
        self.eof = True
        return self._out


register(ZIP_SHRUNK, 'ZIP_SHRUNK', _ShrunkDecompressor)