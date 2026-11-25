#!/usr/bin/env python3
"""
Kindle Paperwhite 3 (7. gen., 2015) offline könyvszinkron.

Munkafolyamat:
  1. A forrásmappa (BookTransfer) e-könyveit AZW3-ra konvertálja a Calibre
     ebook-convert eszközével a helyi gyorsítótárba (_converted). Ez akkor is
     lefut, ha a Kindle nincs csatlakoztatva.
  2. Minden AZW3/MOBI könyvhöz borító-bélyegképet készít (ugyanazzal a
     módszerrel, amit a Calibre Kindle-meghajtója használ), és szintén a
     gyorsítótárba teszi.
  3. Ha a Kindle csatlakoztatva van (USB Mass Storage), a hiányzó könyveket a
     documents mappába másolja, a bélyegképeket pedig a rejtett
     system/thumbnails mappába. A már fent lévő könyvek hiányzó vagy az Amazon
     által lecserélt bélyegképeit is pótolja.

Csak a Python standard library-t használja. Külső függőség: Calibre
(ebook-convert, és opcionálisan calibre-debug a bélyegképek átméretezéséhez).
"""

from __future__ import annotations

import argparse
import locale
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Beállítások
# ---------------------------------------------------------------------------

BOOK_TRANSFER_DIR = Path(r"C:\BookTransfer")
CACHE_DIR_NAME = "_converted"
THUMB_CACHE_DIR_NAME = "_thumbnails"   # a _converted mappán belül
LOG_DIR_NAME = "_logs"                 # a _converted mappán belül

# Calibre telepítési helyek (az első létező nyer, utána a PATH következik)
CALIBRE_DIRS = [
    Path(r"C:\Program Files\Calibre2"),
    Path(r"C:\Program Files (x86)\Calibre2"),
]

OUTPUT_PROFILE = "kindle_pw3"          # 300 ppi, 1072 x 1448 px

# A Calibre "smarten punctuation" angol idézőjeleket tesz ("..." -> “...”),
# a magyar „...” helyett. Ezért alapból ki van kapcsolva.
SMARTEN_PUNCTUATION = False

# Nyelvi metaadat a metaadat nélküli formátumokhoz (szótár, elválasztás).
# None = nem állítja be.
PLAIN_FORMAT_LANGUAGE: str | None = "hu"
PLAIN_FORMATS = {".txt", ".md", ".html", ".htm", ".rtf"}

# True: a könyvek "személyes dokumentumként" (PDOC) kerülnek fel. Ilyenkor a
# Kindle magától mutatja a borítót, bélyegkép nem kell, de a könyvek a
# "Dokumentumok" szűrő alá kerülnek. Alapból False (EBOK + bélyegkép, mint a
# Calibre).
SEND_AS_PERSONAL_DOC = False

CONVERT_TIMEOUT_S = 15 * 60
THUMBNAIL_HEIGHT = 500                 # a Calibre Kindle-meghajtójának értéke
THUMBNAIL_QUALITY = 75

CONVERTIBLE_EXTS = {
    ".epub", ".fb2", ".docx", ".odt", ".rtf",
    ".txt", ".html", ".htm", ".md", ".lit", ".lrf",
}
DIRECT_COPY_EXTS = {".azw3", ".mobi", ".pdf"}
THUMBNAIL_EXTS = {".azw3", ".mobi"}    # ezekhez készülhet bélyegkép

IGNORED_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}

# ---------------------------------------------------------------------------
# Konzol
# ---------------------------------------------------------------------------


def setup_console() -> None:
    """UTF-8 kimenet, hogy az ékezetek a chcp 65001-es konzolon is jók legyenek."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def log(tag: str, msg: str) -> None:
    print(f"[{tag}] {msg}", flush=True)


def log_err(tag: str, msg: str) -> None:
    print(f"[{tag}] {msg}", file=sys.stderr, flush=True)


def decode_output(raw: bytes) -> str:
    """A Calibre kimenete lehet UTF-8 vagy a Windows kódlapja (cp1250)."""
    for enc in ("utf-8", locale.getpreferredencoding(False), "cp1250"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Calibre eszközök
# ---------------------------------------------------------------------------


def find_calibre_tool(name: str) -> Path | None:
    exe = f"{name}.exe" if os.name == "nt" else name
    for d in CALIBRE_DIRS:
        p = d / exe
        if p.is_file():
            return p
    found = shutil.which(name)
    return Path(found) if found else None


# ---------------------------------------------------------------------------
# MOBI / AZW3 fejléc olvasás (EXTH: ASIN, cdetype, borító)
# ---------------------------------------------------------------------------

EXTH_ASIN = 113
EXTH_COVER_OFFSET = 201
EXTH_THUMB_OFFSET = 202
EXTH_CDETYPE = 501
NO_OFFSET = 0xFFFFFFFF


@dataclass
class MobiInfo:
    asin: str | None
    cdetype: str | None
    first_image_index: int | None
    cover_offset: int | None
    thumb_offset: int | None
    record_offsets: list[int] = field(default_factory=list)
    file_size: int = 0

    @property
    def thumbnail_name(self) -> str | None:
        """Ugyanaz a név, amit a Calibre Kindle-meghajtója használ."""
        if self.asin and self.cdetype:
            return f"thumbnail_{self.asin}_{self.cdetype}_portrait.jpg"
        return None


def _read_at(f, offset: int, size: int) -> bytes:
    f.seek(offset)
    data = f.read(size)
    if len(data) != size:
        raise ValueError("váratlan fájlvég")
    return data


def read_mobi_info(path: Path) -> MobiInfo | None:
    """Kiolvassa a MOBI/AZW3 fejlécéből a bélyegképhez szükséges adatokat.

    None-t ad vissza, ha a fájl nem MOBI/AZW3 vagy sérült.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            header = _read_at(f, 0, 78)
            if header[60:68] != b"BOOKMOBI":
                return None
            num_records = struct.unpack(">H", header[76:78])[0]
            if num_records < 2:
                return None
            rec_info = _read_at(f, 78, 8 * num_records)
            offsets = [struct.unpack(">I", rec_info[i * 8:i * 8 + 4])[0] for i in range(num_records)]
            rec0_len = offsets[1] - offsets[0]
            if rec0_len <= 0 or rec0_len > 1 << 20:
                return None
            rec0 = _read_at(f, offsets[0], rec0_len)
    except (OSError, ValueError, struct.error):
        return None

    if rec0[16:20] != b"MOBI":
        return None
    mobi_header_len = struct.unpack(">I", rec0[20:24])[0]
    first_image = None
    if len(rec0) >= 0x70:
        fi = struct.unpack(">I", rec0[0x6C:0x70])[0]
        first_image = None if fi == NO_OFFSET else fi
    has_exth = len(rec0) >= 0x84 and (struct.unpack(">I", rec0[0x80:0x84])[0] & 0x40)

    asin = cdetype = None
    cover_off = thumb_off = None
    if has_exth:
        pos = 16 + mobi_header_len
        if rec0[pos:pos + 4] == b"EXTH":
            count = struct.unpack(">I", rec0[pos + 8:pos + 12])[0]
            p = pos + 12
            for _ in range(count):
                if p + 8 > len(rec0):
                    break
                rtype, rlen = struct.unpack(">II", rec0[p:p + 8])
                if rlen < 8 or p + rlen > len(rec0):
                    break
                content = rec0[p + 8:p + rlen]
                if rtype == EXTH_ASIN:
                    try:
                        asin = content.decode("ascii").strip() or None
                    except UnicodeDecodeError:
                        asin = None
                elif rtype == EXTH_CDETYPE:
                    try:
                        cdetype = content.decode("ascii").strip() or None
                    except UnicodeDecodeError:
                        cdetype = None
                elif rtype == EXTH_COVER_OFFSET and len(content) >= 4:
                    v = struct.unpack(">I", content[:4])[0]
                    cover_off = None if v == NO_OFFSET else v
                elif rtype == EXTH_THUMB_OFFSET and len(content) >= 4:
                    v = struct.unpack(">I", content[:4])[0]
                    thumb_off = None if v == NO_OFFSET else v
                p += rlen

    return MobiInfo(asin, cdetype, first_image, cover_off, thumb_off, offsets, size)


def _image_kind(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def extract_cover(path: Path, info: MobiInfo) -> bytes | None:
    """A beágyazott borítóképet (vagy annak hiányában a bélyegképet) adja vissza."""
    if info.first_image_index is None:
        return None
    offsets = info.record_offsets
    with path.open("rb") as f:
        for rel in (info.cover_offset, info.thumb_offset):
            if rel is None:
                continue
            idx = info.first_image_index + rel
            if idx >= len(offsets):
                continue
            start = offsets[idx]
            end = offsets[idx + 1] if idx + 1 < len(offsets) else info.file_size
            if end <= start:
                continue
            f.seek(start)
            data = f.read(end - start)
            if _image_kind(data):
                return data
    return None


_SCALE_CODE = (
    "import os;"
    "from calibre.utils.img import scale_image;"
    "d=open(os.environ['KS_IN'],'rb').read();"
    "h=int(os.environ['KS_H']);"
    "w2,h2,o=scale_image(d,h,h,compression_quality=int(os.environ['KS_Q']));"
    "open(os.environ['KS_OUT'],'wb').write(o)"
)


def make_thumbnail(cover: bytes, calibre_debug: Path | None) -> bytes | None:
    """Bélyegkép a borítóból: calibre-debug-gal átméretezve, vagy ha az nem
    elérhető, az eredeti JPEG (a Kindle azt is megjeleníti)."""
    if calibre_debug is not None:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "in.img"
            dst = Path(td) / "out.jpg"
            src.write_bytes(cover)
            env = dict(os.environ, KS_IN=str(src), KS_OUT=str(dst),
                       KS_H=str(THUMBNAIL_HEIGHT), KS_Q=str(THUMBNAIL_QUALITY))
            try:
                r = subprocess.run([str(calibre_debug), "-c", _SCALE_CODE],
                                   capture_output=True, env=env, timeout=120)
                if r.returncode == 0 and dst.is_file() and dst.stat().st_size > 0:
                    return dst.read_bytes()
            except (OSError, subprocess.TimeoutExpired):
                pass
    if _image_kind(cover) == "jpeg":
        return cover
    return None


def ensure_cached_thumbnail(book: Path, thumb_cache: Path, calibre_debug: Path | None,
                            dry_run: bool = False) -> tuple[str | None, Path | None, str]:
    """Bélyegképet készít a könyvhöz a helyi gyorsítótárba (ha még nincs).

    Visszatérés: (bélyegkép neve, helyi útvonal, állapot), ahol az állapot:
    'cached' | 'created' | 'pdoc' | 'no-id' | 'no-cover' | 'not-mobi' | 'dry-run'
    """
    info = read_mobi_info(book)
    if info is None:
        return None, None, "not-mobi"
    # PDOC (személyes dokumentum) esetén a Kindle saját maga kezeli a borítót
    if info.cdetype == "PDOC" or (info.cdetype is None and info.asin is None):
        return None, None, "pdoc"
    name = info.thumbnail_name
    if name is None:
        return None, None, "no-id"
    cached = thumb_cache / name
    if cached.is_file() and cached.stat().st_size > 0:
        return name, cached, "cached"
    if dry_run:
        return name, None, "dry-run"
    cover = extract_cover(book, info)
    if cover is None:
        return name, None, "no-cover"
    thumb = make_thumbnail(cover, calibre_debug)
    if thumb is None:
        return name, None, "no-cover"
    thumb_cache.mkdir(parents=True, exist_ok=True)
    tmp = cached.with_suffix(".tmp")
    tmp.write_bytes(thumb)
    os.replace(tmp, cached)
    return name, cached, "created"


# ---------------------------------------------------------------------------
# Tervezés: forrásfájlok -> Kindle-re kerülő fájlok
# ---------------------------------------------------------------------------


@dataclass
class Book:
    source: Path
    output_name: str          # a fájl neve a Kindle-ön
    convert: bool             # True: ebook-convert kell, False: közvetlen másolás
    ready_path: Path          # a felmásolandó fájl helye (forrás vagy cache)


def plan_books(source_dir: Path, cache_dir: Path) -> tuple[list[Book], list[Path]]:
    """Feltérképezi a forrásmappát. Visszatérés: (könyvek, nem támogatott fájlok).

    Névütközés (pl. konyv.epub és konyv.docx, vagy konyv.epub és konyv.azw3)
    esetén a konvertált fájl neve a forrás kiterjesztését is tartalmazza:
    "konyv (epub).azw3".
    """
    books: list[Book] = []
    unsupported: list[Path] = []
    files = sorted(
        (p for p in source_dir.iterdir()
         if p.is_file() and p.name.lower() not in IGNORED_NAMES
         and not p.name.startswith(("~$", "."))),
        key=lambda p: p.name.lower(),
    )
    for p in files:
        ext = p.suffix.lower()
        if ext in DIRECT_COPY_EXTS:
            books.append(Book(p, p.name, False, p))
        elif ext in CONVERTIBLE_EXTS:
            books.append(Book(p, f"{p.stem}.azw3", True, cache_dir / f"{p.stem}.azw3"))
        else:
            unsupported.append(p)

    groups: dict[str, list[Book]] = {}
    for b in books:
        groups.setdefault(b.output_name.lower(), []).append(b)
    for group in groups.values():
        if len(group) < 2:
            continue
        for b in group:
            if b.convert:
                name = f"{b.source.stem} ({b.source.suffix.lower().lstrip('.')}).azw3"
                b.output_name = name
                b.ready_path = cache_dir / name
    return books, unsupported


# ---------------------------------------------------------------------------
# Konvertálás
# ---------------------------------------------------------------------------


def needs_conversion(book: Book) -> bool:
    t = book.ready_path
    return not t.is_file() or t.stat().st_mtime < book.source.stat().st_mtime


def build_convert_cmd(converter: Path, src: Path, dst: Path) -> list[str]:
    cmd = [str(converter), str(src), str(dst), "--output-profile", OUTPUT_PROFILE]
    if SMARTEN_PUNCTUATION:
        cmd.append("--smarten-punctuation")
    if SEND_AS_PERSONAL_DOC:
        cmd.append("--share-not-sync")
    if PLAIN_FORMAT_LANGUAGE and src.suffix.lower() in PLAIN_FORMATS:
        cmd += ["--language", PLAIN_FORMAT_LANGUAGE]
    return cmd


def convert_book(book: Book, converter: Path, log_dir: Path) -> bool:
    target = book.ready_path
    # Ideiglenes név .azw3 végződéssel (az ebook-convert a kiterjesztésből
    # dönti el a kimeneti formátumot). Siker esetén átnevezzük, így egy
    # félbeszakadt konvertálás nem tűnik késznek a következő futáskor.
    partial = target.with_name(target.stem + ".partial.azw3")
    partial.unlink(missing_ok=True)
    cmd = build_convert_cmd(converter, book.source, partial)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=CONVERT_TIMEOUT_S)
        output = decode_output(r.stdout) + decode_output(r.stderr)
        ok = r.returncode == 0 and partial.is_file() and partial.stat().st_size > 0
    except subprocess.TimeoutExpired:
        output, ok = f"Időtúllépés ({CONVERT_TIMEOUT_S} s)", False
    except OSError as exc:
        output, ok = str(exc), False

    if ok:
        os.replace(partial, target)
        (log_dir / f"{book.source.name}.log").unlink(missing_ok=True)
        return True

    partial.unlink(missing_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{book.source.name}.log"
    log_file.write_text(" ".join(cmd) + "\n\n" + output, encoding="utf-8")
    tail = "\n".join(output.strip().splitlines()[-15:])
    log_err("HIBA", f"Sikertelen konvertálás: {book.source.name}\n{tail}\n       Teljes napló: {log_file}")
    return False


def cleanup_partials(cache_dir: Path) -> None:
    for p in cache_dir.glob("*.partial.azw3"):
        try:
            p.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Kindle felismerés
# ---------------------------------------------------------------------------


def _windows_drive_roots() -> list[tuple[Path, str]]:
    """(gyökér, kötetcímke) párok a cserélhető és fix meghajtókra.

    A SetErrorMode megakadályozza a "Nincs lemez a meghajtóban" ablakokat
    (pl. üres kártyaolvasónál).
    """
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    SEM_FAILCRITICALERRORS, SEM_NOOPENFILEERRORBOX = 0x0001, 0x8000
    DRIVE_FIXED, DRIVE_REMOVABLE = 3, 2
    old_mode = kernel32.SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOOPENFILEERRORBOX)
    roots: list[tuple[Path, str]] = []
    try:
        mask = kernel32.GetLogicalDrives()
        for i in range(26):
            if not (mask >> i) & 1:
                continue
            letter = chr(ord("A") + i)
            if letter in "ABC":
                continue
            root = f"{letter}:\\"
            if kernel32.GetDriveTypeW(root) not in (DRIVE_REMOVABLE, DRIVE_FIXED):
                continue
            label_buf = ctypes.create_unicode_buffer(261)
            ok = kernel32.GetVolumeInformationW(root, label_buf, 261, None, None, None, None, 0)
            if not ok:
                continue  # nincs lemez / nem olvasható
            roots.append((Path(root), label_buf.value))
    finally:
        kernel32.SetErrorMode(old_mode)
    return roots


def looks_like_kindle(root: Path, label: str = "") -> bool:
    docs = root / "documents"
    if not docs.is_dir():
        return False
    if label.strip().upper() == "KINDLE":
        return True
    # A 'system' mappa rejtett, de a Python így is látja.
    return (root / "system").is_dir() or (root / ".active_content_sandbox").is_dir()


def find_kindle_root(override: Path | None = None) -> Path | None:
    if override is not None:
        return override if looks_like_kindle(override, "KINDLE") else None
    if os.name != "nt":
        return None
    candidates = [(r, lbl) for r, lbl in _windows_drive_roots() if looks_like_kindle(r, lbl)]
    if not candidates:
        return None
    # A "Kindle" címkéjű meghajtó előnyt élvez
    candidates.sort(key=lambda c: c[1].strip().upper() != "KINDLE")
    if len(candidates) > 1:
        log("FIGYELMEZTETÉS", "Több Kindle-szerű meghajtó: "
            + ", ".join(str(r) for r, _ in candidates) + f" -> {candidates[0][0]} használva")
    return candidates[0][0]


# ---------------------------------------------------------------------------
# Szinkron
# ---------------------------------------------------------------------------


@dataclass
class Stats:
    converted: int = 0
    convert_skipped: int = 0
    convert_failed: int = 0
    copied: int = 0
    copy_skipped: int = 0
    copy_failed: int = 0
    thumbs_written: int = 0
    unsupported: int = 0


def prepare(source_dir: Path, cache_dir: Path, converter: Path | None,
            calibre_debug: Path | None, stats: Stats, dry_run: bool) -> list[Book]:
    if not dry_run:
        source_dir.mkdir(parents=True, exist_ok=True)
        cache_dir.mkdir(parents=True, exist_ok=True)
        cleanup_partials(cache_dir)
    if not source_dir.is_dir():
        log("INFO", f"A forrásmappa nem létezik: {source_dir}")
        return []

    books, unsupported = plan_books(source_dir, cache_dir)
    for p in unsupported:
        log("FIGYELMEZTETÉS", f"Nem támogatott formátum, kihagyva: {p.name}")
    stats.unsupported = len(unsupported)
    if not books:
        log("INFO", f"Nincs feldolgozandó könyv itt: {source_dir}")
        return []

    ready: list[Book] = []
    fresh: set[str] = set()   # ebben a futásban konvertált könyvek
    for b in books:
        if not b.convert:
            ready.append(b)
            continue
        if not needs_conversion(b):
            log("KONVERTÁLÁS KIHAGYVA", f"Már kész: {b.output_name}")
            stats.convert_skipped += 1
            ready.append(b)
            continue
        failed_log = cache_dir / LOG_DIR_NAME / f"{b.source.name}.log"
        if failed_log.is_file() and failed_log.stat().st_mtime >= b.source.stat().st_mtime:
            # Korábban már elbukott, és a forrás azóta nem változott: nem
            # próbáljuk újra minden futáskor. (Újrapróbálás: töröld a naplót.)
            log("KONVERTÁLÁS KIHAGYVA", f"Korábban sikertelen volt: {b.source.name} (napló: {failed_log})")
            stats.convert_failed += 1
            continue
        if converter is None:
            log_err("HIBA", f"Nincs Calibre, nem konvertálható: {b.source.name}")
            stats.convert_failed += 1
            continue
        if dry_run:
            log("KONVERTÁLNÁ", f"{b.source.name} -> {b.output_name}")
            continue
        log("KONVERTÁLÁS", f"{b.source.name} -> {b.output_name} ...")
        if convert_book(b, converter, cache_dir / LOG_DIR_NAME):
            log("OK", f"Konvertálva: {b.output_name}")
            stats.converted += 1
            fresh.add(b.output_name)
            ready.append(b)
        else:
            stats.convert_failed += 1

    # Bélyegképek előkészítése offline is (a Kindle nélkül)
    if not dry_run:
        thumb_cache = cache_dir / THUMB_CACHE_DIR_NAME
        for b in ready:
            if b.ready_path.suffix.lower() in THUMBNAIL_EXTS:
                _, _, state = ensure_cached_thumbnail(b.ready_path, thumb_cache, calibre_debug)
                if state == "no-cover" and b.output_name in fresh:
                    log("INFO", f"Nincs borítókép a könyvben (a Kindle általános borítót mutat): {b.output_name}")
    return ready


def copy_atomic(src: Path, dst: Path) -> None:
    tmp = dst.with_name(dst.name + ".kcopy.tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def sync_books(ready: list[Book], kindle_root: Path, stats: Stats, dry_run: bool) -> None:
    docs = kindle_root / "documents"
    for leftover in docs.glob("*.kcopy.tmp"):
        leftover.unlink(missing_ok=True)
    existing = {p.name.lower(): p for p in docs.rglob("*") if p.is_file()}

    for b in ready:
        on_kindle = existing.get(b.output_name.lower())
        if on_kindle is not None:
            # Felülírás nincs: egy lecserélt fájl új könyvazonosítót kaphat,
            # és elveszhet az olvasási pozíció.
            note = ""
            try:
                local, remote = b.ready_path.stat(), on_kindle.stat()
                if local.st_size != remote.st_size and local.st_mtime > remote.st_mtime + 2:
                    note = " (a helyi változat frissebb; cseréhez töröld a Kindle-ről és futtasd újra)"
            except OSError:
                pass
            log("MÁSOLÁS KIHAGYVA", f"Már rajta van a Kindle-ön: {b.output_name}{note}")
            stats.copy_skipped += 1
            continue
        dest = docs / b.output_name
        size = b.ready_path.stat().st_size
        if not dry_run and shutil.disk_usage(docs).free < size + 5 * 1024 * 1024:
            log_err("HIBA", f"Nincs elég hely a Kindle-ön: {b.output_name}")
            stats.copy_failed += 1
            continue
        if dry_run:
            log("MÁSOLNÁ", f"{b.output_name} -> {dest}")
            continue
        try:
            copy_atomic(b.ready_path, dest)
            log("MÁSOLVA", f"{b.output_name} -> {dest}")
            stats.copied += 1
            existing[b.output_name.lower()] = dest
        except OSError as exc:
            log_err("HIBA", f"Másolás sikertelen ({b.output_name}): {exc}")
            stats.copy_failed += 1


def sync_thumbnails(kindle_root: Path, thumb_cache: Path, calibre_debug: Path | None,
                    stats: Stats, dry_run: bool) -> None:
    """A Kindle-ön lévő összes AZW3/MOBI könyvhöz pótolja a bélyegképet.

    A Kindle-ön lévő fájlból olvassa az azonosítót, így akkor is helyes, ha a
    könyv egy korábbi konvertálásból származik. A már meglévő bélyegképet csak
    akkor írja felül, ha a saját (gyorsítótárban lévő) példányunktól eltér,
    vagyis az Amazon lecserélte egy üres borítóra.
    """
    thumbs_dir = kindle_root / "system" / "thumbnails"
    if not thumbs_dir.is_dir():
        log("FIGYELMEZTETÉS", f"Nincs bélyegkép-mappa a Kindle-ön ({thumbs_dir}), a borítók kimaradnak.")
        return
    existing = {p.name: p for p in thumbs_dir.iterdir() if p.is_file()}
    for book in sorted((kindle_root / "documents").rglob("*")):
        if not book.is_file() or book.suffix.lower() not in THUMBNAIL_EXTS:
            continue
        info = read_mobi_info(book)
        if info is None or info.thumbnail_name is None or info.cdetype == "PDOC":
            continue
        name = info.thumbnail_name
        cached = thumb_cache / name
        on_kindle = existing.get(name)
        if on_kindle is not None and (not cached.is_file()
                                      or cached.stat().st_size == on_kindle.stat().st_size):
            continue  # van bélyegkép, és nem a miénk vagy ugyanaz
        if dry_run:
            log("BORÍTÓ (TERV)", book.name)
            continue
        _, local, state = ensure_cached_thumbnail(book, thumb_cache, calibre_debug)
        if local is None:
            continue
        try:
            copy_atomic(local, thumbs_dir / name)
            stats.thumbs_written += 1
            verb = "visszaállítva" if on_kindle is not None else "feltöltve"
            log("BORÍTÓ", f"{book.name} ({verb})")
        except OSError as exc:
            log_err("HIBA", f"Bélyegkép írása sikertelen ({book.name}): {exc}")


def print_summary(stats: Stats, kindle_connected: bool) -> None:
    print()
    log("ÖSSZESÍTÉS",
        f"Konvertálva: {stats.converted} | Már kész volt: {stats.convert_skipped} | "
        f"Sikertelen: {stats.convert_failed} | Nem támogatott: {stats.unsupported}")
    if kindle_connected:
        log("ÖSSZESÍTÉS",
            f"Másolva: {stats.copied} | Már fent volt: {stats.copy_skipped} | "
            f"Másolási hiba: {stats.copy_failed} | Borító írva: {stats.thumbs_written}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="E-könyvek konvertálása és USB-s szinkronja Kindle PW3-ra.")
    ap.add_argument("--source", type=Path, default=BOOK_TRANSFER_DIR,
                    help=f"forrásmappa (alap: {BOOK_TRANSFER_DIR})")
    ap.add_argument("--kindle", type=Path, default=None,
                    help="a Kindle gyökere (pl. E:\\); alapból automatikus keresés")
    ap.add_argument("--dry-run", action="store_true",
                    help="csak kiírja, mit csinálna; nem konvertál és nem másol")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    setup_console()
    args = parse_args(argv)
    source_dir: Path = args.source
    cache_dir = source_dir / CACHE_DIR_NAME
    stats = Stats()

    converter = find_calibre_tool("ebook-convert")
    calibre_debug = find_calibre_tool("calibre-debug")
    if converter is None:
        log_err("HIBA", "Nem található a Calibre ebook-convert. Telepítsd: https://calibre-ebook.com")
    if calibre_debug is None:
        log("INFO", "calibre-debug nem található, a borítók átméretezés nélkül kerülnek fel.")

    ready = prepare(source_dir, cache_dir, converter, calibre_debug, stats, args.dry_run)

    kindle_root = find_kindle_root(args.kindle)
    if kindle_root is None:
        if args.kindle is not None:
            log_err("HIBA", f"A megadott útvonal nem Kindle (nincs documents mappa): {args.kindle}")
        print()
        log("INFO", "Nincs csatlakoztatott Kindle. A konvertálás kész, másolás nem történt. "
            "Csatlakoztatás után futtasd újra.")
        print_summary(stats, kindle_connected=False)
        return 2 if stats.convert_failed else 0

    print()
    log("INFO", f"Kindle csatlakoztatva: {kindle_root}")
    sync_books(ready, kindle_root, stats, args.dry_run)
    sync_thumbnails(kindle_root, cache_dir / THUMB_CACHE_DIR_NAME, calibre_debug, stats, args.dry_run)
    print_summary(stats, kindle_connected=True)
    if stats.copied or stats.thumbs_written:
        log("INFO", "Kész. Távolítsd el biztonságosan a Kindle-t (Hardver biztonságos eltávolítása), "
            "utána a Kindle beindexeli az új könyveket.")
    return 2 if (stats.convert_failed or stats.copy_failed) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log_err("MEGSZAKÍTVA", "Felhasználói megszakítás.")
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001 - utolsó védvonal, hogy a .bat ablak olvasható maradjon
        log_err("KRITIKUS HIBA", f"{type(exc).__name__}: {exc}")
        sys.exit(1)
