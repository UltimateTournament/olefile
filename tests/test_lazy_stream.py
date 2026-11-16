"""
Tests of OleStream reading a stream's data on demand (cobbery fork).
"""

import io
import struct
import unittest

import olefile
from olefile.olefile import OleFileError

SECTOR = 512
FATSECT, ENDOFCHAIN, FREESECT, NOSTREAM = 0xFFFFFFFD, 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFF


def compound_file(streams):
    """
    A version 3 compound file of the given streams, each a (name, data, sectors)
    tuple: its data, laid out in the given sectors, in that order. Sector 0 holds
    the FAT and sector 1 the directory. Each stream must be 4096 bytes or more, so
    that it lives in the FAT rather than the mini stream.
    """
    sector_count = max(max(sectors) for _, _, sectors in streams) + 1
    assert sector_count <= SECTOR // 4 and len(streams) <= 3
    fat = [FREESECT] * (SECTOR // 4)
    fat[0], fat[1] = FATSECT, ENDOFCHAIN
    body = bytearray(SECTOR * sector_count)
    for _, data, sectors in streams:
        for i, sector in enumerate(sectors):
            fat[sector] = sectors[i + 1] if i + 1 < len(sectors) else ENDOFCHAIN
            piece = data[i * SECTOR:(i + 1) * SECTOR]
            body[sector * SECTOR:sector * SECTOR + len(piece)] = piece
    body[0:SECTOR] = struct.pack('<%dI' % (SECTOR // 4), *fat)

    def entry(name, kind, right, child, start, size):
        encoded = (name + '\0').encode('utf-16-le')
        return struct.pack('<64sHBBIII16sIQQIII', encoded, len(encoded), kind, 1,
                           NOSTREAM, right, child, b'\0' * 16, 0, 0, 0, start, size, 0)

    entries = [entry('Root Entry', 5, NOSTREAM, 1, ENDOFCHAIN, 0)]
    for number, (name, data, sectors) in enumerate(streams, 1):
        right = number + 1 if number < len(streams) else NOSTREAM
        entries.append(entry(name, 2, right, NOSTREAM, sectors[0], len(data)))
    while len(entries) < 4:
        entries.append(struct.pack('<64sHBBIII', b'', 0, 0, 0, NOSTREAM, NOSTREAM, NOSTREAM) + b'\0' * 48)
    directory = b''.join(entries)
    assert len(directory) == SECTOR
    body[SECTOR:2 * SECTOR] = directory
    header = struct.pack('<8s16sHHHHH6sIIIIIIIII', olefile.MAGIC, b'\0' * 16, 0x3E, 3, 0xFFFE,
                         9, 6, b'\0' * 6, 0, 1, 1, 0, 4096, ENDOFCHAIN, 0, ENDOFCHAIN, 0)
    header += struct.pack('<109I', 0, *([FREESECT] * 108))
    return header + bytes(body)


# A sits in every other sector. B takes the sectors in between, then 20 adjacent
# ones, and ends inside its last sector.
A = bytes(bytearray(i % 251 for i in range(20 * SECTOR)))
B = bytes(bytearray((i * 7) % 253 for i in range(40 * SECTOR - 480)))
A_SECTORS = list(range(2, 42, 2))
B_SECTORS = list(range(3, 42, 2)) + list(range(42, 62))
FILE = compound_file([('A', A, A_SECTORS), ('B', B, B_SECTORS)])


class CountingFile(object):
    """A file object that counts the reads made of it."""

    def __init__(self, data):
        self._file = io.BytesIO(data)
        self.reads = 0
        self.closed = False

    def read(self, size=-1):
        self.reads += 1
        return self._file.read(size)

    def seek(self, offset, whence=0):
        return self._file.seek(offset, whence)

    def tell(self):
        return self._file.tell()


class LazyStreamTest(unittest.TestCase):

    def test_reads_whole_streams(self):
        with olefile.OleFileIO(FILE) as ole:
            for name, data in (('A', A), ('B', B)):
                stream = ole.openstream(name)
                self.assertEqual(stream.size, len(data))
                self.assertEqual(stream.read(), data)
                self.assertEqual(stream.read(), b'')

    def test_reads_full_pieces_across_sectors(self):
        with olefile.OleFileIO(FILE) as ole:
            stream = ole.openstream('B')
            pieces = []
            while True:
                piece = stream.read(1000)
                if not piece:
                    break
                # every piece but the last is whole, also across sectors that
                # are not adjacent:
                self.assertTrue(len(piece) == 1000 or stream.tell() == len(B))
                pieces.append(piece)
            self.assertEqual(b''.join(pieces), B)
            buffer = bytearray(700)
            stream.seek(300)
            self.assertEqual(stream.readinto(buffer), 700)
            self.assertEqual(bytes(buffer), B[300:1000])

    def test_seeks(self):
        with olefile.OleFileIO(FILE) as ole:
            stream = ole.openstream('A')
            self.assertEqual(stream.seek(1500), 1500)
            self.assertEqual(stream.read(100), A[1500:1600])
            self.assertEqual(stream.seek(-200, io.SEEK_CUR), 1400)
            self.assertEqual(stream.read(10), A[1400:1410])
            self.assertEqual(stream.seek(-10, io.SEEK_END), len(A) - 10)
            self.assertEqual(stream.read(), A[-10:])
            self.assertEqual(stream.seek(len(A) + 5), len(A) + 5)
            self.assertEqual(stream.read(), b'')
            self.assertRaises(ValueError, stream.seek, -1)

    def test_reads_data_only_when_asked_and_adjacent_sectors_at_once(self):
        spy = CountingFile(FILE)
        ole = olefile.OleFileIO(spy)
        reads = spy.reads
        stream = ole.openstream('B')
        self.assertEqual(spy.reads, reads)
        self.assertEqual(stream.read(), B)
        # one read for each of the first 19 sectors between A's; the 20th lies
        # before the 20 adjacent ones and is read with them:
        self.assertEqual(spy.reads, reads + 20)
        ole.close()

    def test_a_chain_shorter_than_its_stream_is_a_defect(self):
        # B declares more data than its 40 sectors hold.
        broken = compound_file([('A', A, A_SECTORS), ('B', B + b'x' * 10000, B_SECTORS)])
        with olefile.OleFileIO(broken) as ole:
            stream = ole.openstream('B')
            self.assertEqual(stream.size, 40 * SECTOR)
            self.assertEqual(stream.read()[:len(B)], B)
            self.assertTrue(ole.parsing_issues)
        with olefile.OleFileIO(broken, raise_defects=olefile.DEFECT_INCORRECT) as ole:
            self.assertRaises(OleFileError, ole.openstream, 'B')

    def test_a_stream_past_the_end_of_the_file_is_a_defect(self):
        # The file ends after B's sector 43.
        truncated = FILE[:SECTOR * (1 + 44)]
        with olefile.OleFileIO(truncated) as ole:
            stream = ole.openstream('B')
            self.assertEqual(stream.read(), B[:stream.size])
            self.assertTrue(ole.parsing_issues)
        with olefile.OleFileIO(truncated, raise_defects=olefile.DEFECT_INCORRECT) as ole:
            self.assertRaises(OleFileError, ole.openstream, 'B')

    def test_a_closed_stream_cannot_be_read(self):
        with olefile.OleFileIO(FILE) as ole:
            stream = ole.openstream('A')
            stream.close()
            self.assertRaises(ValueError, stream.read, 10)
            self.assertRaises(ValueError, stream.seek, 0)


if __name__ == '__main__':
    unittest.main()
