"""Tesztek a sync_kindle.py-hoz. Futtatás: python -m unittest discover -s tests

Csak standard library. Az integrációs teszt csak akkor fut, ha a Calibre
ebook-convert elérhető.
"""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sync_kindle as sk  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"fakejpeg" * 10


def build_mobi(asin: str | None = "abc-123", cdetype: str | None = "EBOK",
               cover_offset: int | None = 0, image: bytes = JPEG) -> bytes:
    """Minimális, a fejlécolvasónak elég PalmDB/MOBI fájl."""
    exth_recs = b""
    count = 0
    if asin is not None:
        d = asin.encode()
        exth_recs += struct.pack(">II", 113, len(d) + 8) + d
        count += 1
    if cdetype is not None:
        d = cdetype.encode()
        exth_recs += struct.pack(">II", 501, len(d) + 8) + d
        count += 1
    if cover_offset is not None:
        exth_recs += struct.pack(">III", 201, 12, cover_offset)
        count += 1
    exth = b"EXTH" + struct.pack(">II", 12 + len(exth_recs), count) + exth_recs

    mobi_header_len = 0xE8
    mobi = bytearray(mobi_header_len)
    mobi[0:4] = b"MOBI"
    mobi[4:8] = struct.pack(">I", mobi_header_len)
    first_image = 2  # rec0, rec1 = szöveg, rec2 = kép
    # a 0x6C és 0x80 eltolás a record 0 elejétől értendő (16 bájt PalmDOC fejléc után)
    mobi[0x6C - 16:0x70 - 16] = struct.pack(">I", first_image)
    mobi[0x80 - 16:0x84 - 16] = struct.pack(">I", 0x40)
    rec0 = b"\x00" * 16 + bytes(mobi) + exth
    records = [rec0, b"text record", image]

    n = len(records)
    header = bytearray(78)
    header[0:4] = b"test"
    header[60:68] = b"BOOKMOBI"
    header[76:78] = struct.pack(">H", n)
    offset = 78 + 8 * n + 2
    rec_list = b""
    for i, r in enumerate(records):
        rec_list += struct.pack(">II", offset, i)
        offset += len(r)
    return bytes(header) + rec_list + b"\x00\x00" + b"".join(records)


class MobiParsingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name: str, data: bytes) -> Path:
        p = self.dir / name
        p.write_bytes(data)
        return p

    def test_reads_asin_cdetype_and_cover(self):
        p = self.write("a.azw3", build_mobi())
        info = sk.read_mobi_info(p)
        self.assertIsNotNone(info)
        self.assertEqual(info.thumbnail_name, "thumbnail_abc-123_EBOK_portrait.jpg")
        self.assertEqual(sk.extract_cover(p, info), JPEG)

    def test_not_mobi(self):
        p = self.write("x.pdf", b"%PDF-1.4 hello" + b"\x00" * 100)
        self.assertIsNone(sk.read_mobi_info(p))

    def test_truncated_file(self):
        p = self.write("t.azw3", build_mobi()[:90])
        self.assertIsNone(sk.read_mobi_info(p))

    def test_pdoc_needs_no_thumbnail(self):
        p = self.write("p.azw3", build_mobi(asin=None, cdetype=None))
        name, local, state = sk.ensure_cached_thumbnail(p, self.dir / "thumbs", None)
        self.assertEqual((name, local, state), (None, None, "pdoc"))

    def test_thumbnail_cached_without_calibre_debug(self):
        p = self.write("a.azw3", build_mobi())
        name, local, state = sk.ensure_cached_thumbnail(p, self.dir / "thumbs", None)
        self.assertEqual(state, "created")
        self.assertEqual(local.read_bytes(), JPEG)
        _, _, state2 = sk.ensure_cached_thumbnail(p, self.dir / "thumbs", None)
        self.assertEqual(state2, "cached")

    def test_no_cover(self):
        p = self.write("n.azw3", build_mobi(cover_offset=None))
        _, _, state = sk.ensure_cached_thumbnail(p, self.dir / "thumbs", None)
        self.assertEqual(state, "no-cover")


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = Path(self.tmp.name)
        self.cache = self.src / "_converted"

    def tearDown(self):
        self.tmp.cleanup()

    def touch(self, *names: str) -> None:
        for n in names:
            (self.src / n).write_bytes(b"x")

    def names(self):
        books, unsupported = sk.plan_books(self.src, self.cache)
        return {b.source.name: b.output_name for b in books}, [p.name for p in unsupported]

    def test_basic(self):
        self.touch("a.epub", "b.pdf", "c.MOBI", "d.xyz", "desktop.ini", "~$temp.docx")
        names, unsupported = self.names()
        self.assertEqual(names, {"a.epub": "a.azw3", "b.pdf": "b.pdf", "c.MOBI": "c.MOBI"})
        self.assertEqual(unsupported, ["d.xyz"])

    def test_collision_two_convertibles(self):
        self.touch("konyv.epub", "konyv.docx")
        names, _ = self.names()
        self.assertEqual(names, {"konyv.epub": "konyv (epub).azw3", "konyv.docx": "konyv (docx).azw3"})

    def test_collision_with_direct_copy(self):
        self.touch("konyv.epub", "Konyv.azw3")
        names, _ = self.names()
        self.assertEqual(names["Konyv.azw3"], "Konyv.azw3")
        self.assertEqual(names["konyv.epub"], "konyv (epub).azw3")

    def test_needs_conversion_by_mtime(self):
        self.touch("a.epub")
        self.cache.mkdir()
        book = sk.plan_books(self.src, self.cache)[0][0]
        self.assertTrue(sk.needs_conversion(book))
        book.ready_path.write_bytes(b"done")
        old = time.time() - 100
        os.utime(book.source, (old, old))
        self.assertFalse(sk.needs_conversion(book))
        new = time.time() + 100
        os.utime(book.source, (new, new))
        self.assertTrue(sk.needs_conversion(book))


class CommandTests(unittest.TestCase):
    def test_default_flags(self):
        cmd = sk.build_convert_cmd(Path("ebook-convert"), Path("a.epub"), Path("a.azw3"))
        self.assertIn("kindle_pw3", cmd)
        self.assertNotIn("--no-inline-toc", cmd)
        self.assertNotIn("--smarten-punctuation", cmd)
        self.assertNotIn("--language", cmd)

    def test_plain_format_gets_language(self):
        cmd = sk.build_convert_cmd(Path("ebook-convert"), Path("a.txt"), Path("a.azw3"))
        self.assertEqual(cmd[cmd.index("--language") + 1], "hu")

    def test_decode_output(self):
        self.assertEqual(sk.decode_output("árvíztűrő".encode("utf-8")), "árvíztűrő")
        self.assertIsInstance(sk.decode_output("hiba ő".encode("cp1250")), str)


class KindleDetectionTests(unittest.TestCase):
    def test_looks_like_kindle(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertFalse(sk.looks_like_kindle(root))
            (root / "documents").mkdir()
            self.assertFalse(sk.looks_like_kindle(root))
            self.assertTrue(sk.looks_like_kindle(root, "Kindle"))
            (root / "system").mkdir()
            self.assertTrue(sk.looks_like_kindle(root))


class SyncTests(unittest.TestCase):
    def test_copy_skip_and_thumbnail_restore(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            src = base / "src"
            src.mkdir()
            (src / "konyv.azw3").write_bytes(build_mobi())
            kindle = base / "kindle"
            (kindle / "documents" / "sub").mkdir(parents=True)
            (kindle / "system" / "thumbnails").mkdir(parents=True)
            (kindle / "documents" / "sub" / "MAR_FENT.pdf").write_bytes(b"%PDF")
            (src / "mar_fent.pdf").write_bytes(b"%PDF")

            rc = sk.main(["--source", str(src), "--kindle", str(kindle)])
            self.assertEqual(rc, 0)
            self.assertTrue((kindle / "documents" / "konyv.azw3").is_file())
            self.assertFalse((kindle / "documents" / "mar_fent.pdf").exists())
            thumb = kindle / "system" / "thumbnails" / "thumbnail_abc-123_EBOK_portrait.jpg"
            self.assertEqual(thumb.read_bytes(), JPEG)

            # Az "Amazon cover bug" szimulálása: a bélyegképet lecseréli
            thumb.write_bytes(b"generic")
            sk.main(["--source", str(src), "--kindle", str(kindle)])
            self.assertEqual(thumb.read_bytes(), JPEG)

    def test_no_kindle_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            src.mkdir()
            (src / "a.pdf").write_bytes(b"%PDF")
            if os.name != "nt":
                self.assertEqual(sk.main(["--source", str(src)]), 0)


@unittest.skipIf(sk.find_calibre_tool("ebook-convert") is None, "Calibre nincs telepítve")
class CalibreIntegrationTests(unittest.TestCase):
    def test_txt_conversion_has_ebok_id(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            src.mkdir()
            (src / "mese.txt").write_text("Árvíztűrő tükörfúrógép.\n", encoding="utf-8")
            kindle = Path(td) / "kindle"
            (kindle / "documents").mkdir(parents=True)
            (kindle / "system" / "thumbnails").mkdir(parents=True)
            self.assertEqual(sk.main(["--source", str(src), "--kindle", str(kindle)]), 0)
            out = kindle / "documents" / "mese.azw3"
            info = sk.read_mobi_info(out)
            self.assertIsNotNone(info)
            self.assertEqual(info.cdetype, "EBOK")
            self.assertTrue(info.asin)


if __name__ == "__main__":
    unittest.main()
