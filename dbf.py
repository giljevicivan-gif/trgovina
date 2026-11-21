# -*- coding: utf-8 -*-
"""Čitač Visual FoxPro .DBF datoteka (iz programa Roba). Samo čitanje."""

import datetime as dt
import os
import struct

ENCODING = "cp1250"  # FoxPro, srednjoeuropska kodna stranica


def _jd_to_date(jd):
    # Julijanski dan -> datum (FoxPro 'T' polja)
    if jd <= 0:
        return None
    return dt.date(1, 1, 1) + dt.timedelta(days=jd - 1721426)


def read_dbf(path, polja=None):
    """Pročita Visual FoxPro .DBF i vrati listu rječnika. Samo čitanje."""
    with open(path, "rb") as f:
        data = f.read()
    return read_dbf_bytes(data, polja)


def read_dbf_bytes(data, polja=None):
    """Isto kao read_dbf, iz već učitanih bajtova (npr. iz .zip arhive).
    polja = skup imena polja koja trebaju (brže za velike datoteke), None = sva."""
    count = struct.unpack("<I", data[4:8])[0]
    header_len, rec_len = struct.unpack("<HH", data[8:12])
    fields, pos = [], 32
    while pos < header_len and data[pos] != 0x0D:
        name = data[pos:pos + 11].split(b"\0")[0].decode("ascii", "replace")
        ftype = chr(data[pos + 11])
        flen = data[pos + 16]
        fields.append((name, ftype, flen))
        pos += 32

    rows = []
    for i in range(count):
        start = header_len + i * rec_len
        rec = data[start:start + rec_len]
        if len(rec) < rec_len or rec[0:1] == b"*":  # obrisani zapis
            continue
        row, off = {}, 1
        for name, ftype, flen in fields:
            raw = rec[off:off + flen]
            off += flen
            if polja is not None and name not in polja:
                continue
            if ftype == "C":
                row[name] = raw.decode(ENCODING, "replace").rstrip()
            elif ftype in "NF":
                s = raw.strip()
                try:
                    row[name] = float(s) if s and b"*" not in s else 0.0
                except ValueError:
                    row[name] = 0.0
            elif ftype == "I":
                row[name] = struct.unpack("<i", raw)[0]
            elif ftype == "D":
                s = raw.decode("ascii", "replace").strip()
                row[name] = f"{s[0:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else None
            elif ftype == "T":
                jd, ms = struct.unpack("<ii", raw)
                d = _jd_to_date(jd)
                row[name] = (dt.datetime.combine(d, dt.time()) +
                             dt.timedelta(milliseconds=ms)) if d else None
            elif ftype == "L":
                row[name] = raw in (b"T", b"t", b"Y", b"y")
            elif ftype == "Y":
                row[name] = struct.unpack("<q", raw)[0] / 10000.0
            elif ftype == "B":
                row[name] = struct.unpack("<d", raw)[0]
            # M, G, 0 (_NullFlags) ... preskačemo
        rows.append(row)
    return rows


def find_file(folder, name):
    """Windows ne razlikuje velika/mala slova, ali ovako radi svugdje."""
    want = name.lower()
    try:
        for f in os.listdir(folder):
            if f.lower() == want:
                return os.path.join(folder, f)
    except OSError:
        pass
    return None
