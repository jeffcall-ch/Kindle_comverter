# Kindle_comverter

Egykattintásos e-könyv konvertáló és USB-s szinkron egy **offline** Kindle
Paperwhite 3-ra (7. generáció, 2015). Windows 11, Python 3.10+, Calibre.

A `C:\BookTransfer` mappába bedobott könyveket AZW3-ra konvertálja, és ha a
Kindle csatlakoztatva van, felmásolja a hiányzókat **borítóképpel együtt**.

## Telepítés

1. **Python 3.10+**: <https://www.python.org/downloads/> (telepítéskor a „py launcher” maradjon bejelölve).
2. **Calibre**: <https://calibre-ebook.com/download_windows> (az alapértelmezett helyre, `C:\Program Files\Calibre2`).
3. A repót klónozd vagy töltsd le egy állandó helyre, pl. `C:\Tools\Kindle_comverter`.
4. Jobb klikk a `Kindle_szinkron.bat` fájlon → *További lehetőségek* → *Küldés* → *Asztal (parancsikon létrehozása)*.

A `C:\BookTransfer` mappát a script első futáskor létrehozza.

## Használat

1. Másold a könyveket a `C:\BookTransfer` mappába.
2. Csatlakoztasd a Kindle-t USB-n (nem kötelező, a konvertálás nélküle is lefut).
3. Indítsd az asztali parancsikont.
4. A végén **távolítsd el biztonságosan** a Kindle-t. Ezután indexeli be az új könyveket.

Parancssori kapcsolók (`py sync_kindle.py --help`):

| Kapcsoló | Jelentés |
|---|---|
| `--source MAPPA` | más forrásmappa |
| `--kindle E:\` | a Kindle meghajtó kézi megadása |
| `--dry-run` | csak kiírja, mit csinálna |

## Formátumok

| Típus | Kiterjesztések |
|---|---|
| AZW3-ra konvertál | `.epub .fb2 .docx .odt .rtf .txt .html .htm .md .lit .lrf` |
| Változatlanul másol | `.azw3 .mobi .pdf` |

## A Kindle beállítása

- **Normál profil + szülői felügyelet**, ne Amazon Kids (FreeTime) profil. A gyerekprofil nem látja az USB-n felmásolt könyveket.
- Repülőgép üzemmód maradjon bekapcsolva. Így az Amazon nem tudja lecserélni a borítókat.

## Hogyan működik

1. **Konvertálás** a `C:\BookTransfer\_converted` gyorsítótárba. Csak akkor konvertál újra, ha a forrás újabb, mint a kész AZW3. A konvertálás ideiglenes néven fut, és csak siker után kapja meg a végleges nevet, így egy félbeszakadt futás nem hagy „késznek látszó” hibás fájlt.
2. **Borító-bélyegképek.** A Kindle az USB-n felmásolt könyveknél nem a fájlba ágyazott borítót mutatja, hanem a rejtett `system\thumbnails` mappában keres egy `thumbnail_<ASIN>_EBOK_portrait.jpg` nevű képet. A script ugyanazt csinálja, mint a Calibre Kindle-meghajtója: kiolvassa a könyv fejlécéből az azonosítót és a borítót, 500 px magasra méretezi (`calibre-debug` segítségével), és oda írja. A bélyegképek a `_converted\_thumbnails` mappában is megmaradnak, így ha az Amazon mégis lecserélné őket üres borítóra, a következő futás visszaállítja.
3. **Másolás.** A `documents` mappát (almappákkal együtt) végignézi, és csak a hiányzó fájlokat másolja a gyökerébe. A nevek egyeztetése kis- és nagybetűtől független.

## Tudatos döntések az eredeti spechez képest

- **Nincs `--no-inline-toc`.** A Calibre FAQ szerint a fájl végi tartalomjegyzék kikapcsolása elronthat Kindle-funkciókat (pl. Page Flip).
- **Nincs `--smarten-punctuation`.** Angol idézőjeleket tesz be, a magyar „…” helyett. A `SMARTEN_PUNCTUATION` beállítással visszakapcsolható.
- **Nyelv:** a `.txt`, `.md`, `.html`, `.htm` és `.rtf` fájlok `hu` nyelvi metaadatot kapnak (`PLAIN_FORMAT_LANGUAGE`).
- **Névütközés:** ha `konyv.epub` és `konyv.docx` (vagy `konyv.azw3`) is van, a konvertált fájlok neve `konyv (epub).azw3` lesz.
- **Nincs felülírás.** Ha egy könyv már fent van, nem írja felül, mert az új fájl új azonosítót kaphat, és elveszhet az olvasási pozíció. Ha a helyi változat frissebb, a script szól. Cseréhez töröld a régit a Kindle-ről, és futtasd újra.
- **Hibás forrásfájl:** a sikertelen konvertálás naplója a `_converted\_logs` mappába kerül. Amíg a forrás nem változik, a script nem próbálja újra (újrapróbáláshoz töröld a naplót).
- **Kindle-felismerés:** cserélhető vagy fix meghajtó, amelyen van `documents` mappa, és vagy „Kindle” a kötetcímkéje, vagy van rajta `system` mappa. Az üres kártyaolvasók nem dobnak „Nincs lemez” ablakot.

## Beállítások

A `sync_kindle.py` elején:

| Név | Alap | Leírás |
|---|---|---|
| `BOOK_TRANSFER_DIR` | `C:\BookTransfer` | forrásmappa |
| `OUTPUT_PROFILE` | `kindle_pw3` | Calibre kimeneti profil |
| `SMARTEN_PUNCTUATION` | `False` | lásd fent |
| `PLAIN_FORMAT_LANGUAGE` | `"hu"` | nyelv a metaadat nélküli formátumokhoz |
| `SEND_AS_PERSONAL_DOC` | `False` | `True`: „személyes dokumentumként” (PDOC) kerül fel. A Kindle így magától mutatja a borítót, de a könyvek a *Dokumentumok* szűrő alá kerülnek. |
| `THUMBNAIL_HEIGHT` | `500` | bélyegkép magassága (a Calibre értéke) |

## Korlátok

- Csak hozzáad: ha a `BookTransfer`-ből törölsz, a Kindle-ről nem törlődik.
- A borító nélküli forrásoknál (pl. `.txt`) a Kindle általános borítót mutat.
- A közvetlenül másolt `.azw3`/`.mobi` fájlok csak akkor kapnak bélyegképet, ha van bennük ASIN és cdetype (pl. Calibre készítette őket).

## Tesztek

```
py -m unittest discover -s tests
```

Csak standard library. A Calibre-es integrációs teszt automatikusan kimarad, ha az `ebook-convert` nincs telepítve.
