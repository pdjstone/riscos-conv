import zipfile

from .zip_codec import register

# ZIP compression method 6 (Implode). Python's zipfile does not support it,
# so the decompressor is registered with zip_codec, which monkey-patches
# zipfile to use it. The algorithm is a port of the Explode decoder from Jason
# Summers' "hwzip" series (Shrink, Reduce, and Implode -- The Legacy Zip
# Compression Methods).
ZIP_IMPLODE = 6

IMP_MAX_HUFFMAN_BITS = 16
IMP_LOOKUP_TABLE_BITS = 8


def _lsb(x, n):
    return x & ((1 << n) - 1)


def _reverse16(x, n):
    """Reverse the low n bits of x (the hwzip reverse16 helper)."""
    rev = 0
    for _ in range(16):
        rev = (rev << 1) | (x & 1)
        x >>= 1
    return rev >> (16 - n)


class _HuffmanDecoder:
    def __init__(self):
        self.table = [None] * (1 << IMP_LOOKUP_TABLE_BITS)  # (sym, len) or None
        self.sentinel_bits = [0] * (IMP_MAX_HUFFMAN_BITS + 1)
        self.offset_first_sym_idx = [0] * (IMP_MAX_HUFFMAN_BITS + 1)
        self.syms = [0] * 288


def _table_insert(d, sym, length, codeword):
    """Make the codeword LSB-first and place it in the lookup table."""
    codeword = _reverse16(codeword, length) & 0xffff
    pad_len = IMP_LOOKUP_TABLE_BITS - length
    for padding in range(1 << pad_len):
        index = codeword | (padding << length)
        d.table[index] = (sym, length)


def _huffman_decoder_init(d, lengths, n):
    count = [0] * (IMP_MAX_HUFFMAN_BITS + 1)
    code = [0] * (IMP_MAX_HUFFMAN_BITS + 1)
    sym_idx = [0] * (IMP_MAX_HUFFMAN_BITS + 1)
    for ln in lengths:
        count[ln] += 1
    count[0] = 0

    for l in range(1, IMP_MAX_HUFFMAN_BITS + 1):
        code[l] = ((code[l - 1] + count[l - 1]) << 1) & 0xffff
        if count[l] != 0 and code[l] + count[l] - 1 > (1 << l) - 1:
            return False
        d.sentinel_bits[l] = (code[l] + count[l]) << (IMP_MAX_HUFFMAN_BITS - l)
        sym_idx[l] = (sym_idx[l - 1] + count[l - 1]) & 0xffff
        d.offset_first_sym_idx[l] = (sym_idx[l] - code[l]) & 0xffff

    for i in range(n):
        l = lengths[i]
        if l == 0:
            continue
        d.syms[sym_idx[l] & 0xffff] = i
        sym_idx[l] = (sym_idx[l] + 1) & 0xffff
        if l <= IMP_LOOKUP_TABLE_BITS:
            _table_insert(d, i, l, code[l])
            code[l] = (code[l] + 1) & 0xffff
    return True


def _huffman_decode(d, bits, used):
    """Decode a symbol from the LSB-first zero-padded bits (hwzip huffman_decode)."""
    entry = d.table[bits & 0xff]
    if entry is not None:
        used[0] = entry[1]
        return entry[0]
    bits = _reverse16(bits, IMP_MAX_HUFFMAN_BITS) & 0xffff
    for l in range(IMP_LOOKUP_TABLE_BITS + 1, IMP_MAX_HUFFMAN_BITS + 1):
        if bits < d.sentinel_bits[l]:
            bits >>= IMP_MAX_HUFFMAN_BITS - l
            sym_idx = (d.offset_first_sym_idx[l] + bits) & 0xffff
            used[0] = l
            return d.syms[sym_idx]
    used[0] = 0
    return -1


class _Istream:
    """LSB-first input bitstream over a byte buffer (hwzip istream_t)."""

    def __init__(self, src):
        self.src = src
        self.bitpos = 0
        self.bitpos_end = len(src) * 8

    def bits(self):
        """Return up to 64 bits from the current position, zero-padded."""
        n = self.bitpos >> 3
        chunk = self.src[n:n + 8]
        v = int.from_bytes(chunk, 'little') if chunk else 0
        return v >> (self.bitpos & 7)

    def advance(self, n):
        if self.bitpos_end - self.bitpos < n:
            return False
        self.bitpos += n
        return True


def _read_huffman_code(is_, num_lens):
    """Read a Huffman code description (num_lens codeword lengths) from is."""
    byte = _lsb(is_.bits(), 8)
    num_bytes = byte + 1
    if not is_.advance(8):
        return None

    lens = []
    len_count = [0] * 17
    codeword_idx = 0
    for _ in range(num_bytes):
        byte = _lsb(is_.bits(), 8)
        if not is_.advance(8):
            return None
        codeword_len = (byte & 0xf) + 1
        run_length = (byte >> 4) + 1
        len_count[codeword_len] += run_length
        if codeword_idx + run_length > num_lens:
            return None
        lens.extend([codeword_len] * run_length)
        codeword_idx += run_length

    if codeword_idx < num_lens:
        return None

    # The Huffman tree must be "full" (all codewords used).
    avail_codewords = 1
    for i in range(1, 17):
        avail_codewords = avail_codewords * 2 - len_count[i]
        if avail_codewords < 0:
            return None
    if avail_codewords != 0:
        return None

    d = _HuffmanDecoder()
    if not _huffman_decoder_init(d, lens, num_lens):
        return None
    return d


def _copy_backref(dst, dst_pos, dist, length):
    """Copy a back reference into dst at dst_pos, handling overlap and
    implicit zeros (when dist > dst_pos)."""
    if dist <= dst_pos:
        if length <= dist:
            dst.extend(dst[dst_pos - dist:dst_pos - dist + length])
            dst_pos += length
        else:
            for _ in range(length):
                dst.append(dst[dst_pos - dist])
                dst_pos += 1
    else:
        for _ in range(length):
            if dist > dst_pos:
                dst.append(0)
            else:
                dst.append(dst[dst_pos - dist])
            dst_pos += 1
    return dst_pos


def _explode(src, uncomp_len, large_wnd, lit_tree):
    """Decompress src (ZIP method 6) into exactly uncomp_len bytes.

    large_wnd: 8K sliding dictionary in effect (else 4K).
    lit_tree:  literals are Huffman coded (else raw 8-bit literals).
    """
    is_ = _Istream(src)
    if lit_tree:
        lit_dec = _read_huffman_code(is_, 256)
        if lit_dec is None:
            raise zipfile.BadZipFile("bad lit Huffman code")
    else:
        lit_dec = None
    len_dec = _read_huffman_code(is_, 64)
    if len_dec is None:
        raise zipfile.BadZipFile("bad len Huffman code")
    dist_dec = _read_huffman_code(is_, 64)
    if dist_dec is None:
        raise zipfile.BadZipFile("bad dist Huffman code")

    min_len = 3 if lit_tree else 2
    dst = bytearray()
    dst_pos = 0

    while dst_pos < uncomp_len:
        bits = is_.bits()

        if _lsb(bits, 1) == 0x1:
            # Literal.
            bits >>= 1
            if lit_tree:
                used = [0]
                sym = _huffman_decode(lit_dec, (~bits) & 0xffff, used)
                if sym < 0:
                    raise zipfile.BadZipFile("bad literal")
                if not is_.advance(1 + used[0]):
                    raise zipfile.BadZipFile("truncated literal")
            else:
                sym = _lsb(bits, 8)
                if not is_.advance(1 + 8):
                    raise zipfile.BadZipFile("truncated literal")
            if sym > 255:
                raise zipfile.BadZipFile("bad literal value")
            dst.append(sym)
            dst_pos += 1
            continue

        # Backref.
        used_tot = 1
        bits >>= 1
        if large_wnd:
            dist = _lsb(bits, 7)
            bits >>= 7
            used_tot += 7
        else:
            dist = _lsb(bits, 6)
            bits >>= 6
            used_tot += 6

        used = [0]
        sym = _huffman_decode(dist_dec, (~bits) & 0xffff, used)
        if sym < 0:
            raise zipfile.BadZipFile("bad dist")
        used_tot += used[0]
        bits >>= used[0]
        dist |= sym << (7 if large_wnd else 6)
        dist += 1

        used = [0]
        sym = _huffman_decode(len_dec, (~bits) & 0xffff, used)
        if sym < 0:
            raise zipfile.BadZipFile("bad len")
        used_tot += used[0]
        bits >>= used[0]
        length = sym + min_len

        if sym == 63:
            # Extra len byte.
            length += _lsb(bits, 8)
            used_tot += 8
            bits >>= 8

        if not is_.advance(used_tot):
            raise zipfile.BadZipFile("truncated backref")
        if length > uncomp_len - dst_pos:
            raise zipfile.BadZipFile("backref overruns output")
        dst_pos = _copy_backref(dst, dst_pos, dist, length)

    return bytes(dst)


class _ImplodeDecompressor:
    """zipfile-compatible decompressor for method 6.

    Buffers the whole compressed member (the implode format needs all input
    to decode), then emits the decompressed data in one go.
    """

    def __init__(self):
        self.eof = False
        self._buf = bytearray()
        self._compress_size = None
        self._file_size = None
        self._flag_bits = 0
        self._out = b''

    def set_info(self, flag_bits, compress_size, file_size):
        self._flag_bits = flag_bits
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
        large_wnd = bool(self._flag_bits & 0x0002)
        lit_tree = bool(self._flag_bits & 0x0004)
        self._out = _explode(bytes(self._buf[:self._compress_size]),
                             self._file_size, large_wnd, lit_tree)
        self.eof = True
        return self._out


register(ZIP_IMPLODE, 'ZIP_IMPLODE', _ImplodeDecompressor)
