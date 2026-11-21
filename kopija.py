# -*- coding: utf-8 -*-
"""
Sigurnosna kopija naše baze (trgovina.db).

Kopija se radi sama kod svakog pokretanja i na gumb u programu, u mapu
"sigurnosne_kopije" pokraj programa. Čuva se zadnjih 30 kopija i zauvijek zadnja
kopija svake godine (poslovne knjige i isprave čuvaju se godinama, Zakon o računovodstvu).
Ako je u pokreni.bat postavljena TRGOVINA_KOPIJA_MAPA (npr. USB ili OneDrive),
kopija se sprema i tamo.

Kopija se radi preko SQLite "backup" funkcije, pa je ispravna i dok kasa radi.
Vraćanje: zatvorite kasu, kopirajte željenu kopiju preko trgovina.db.
"""

import datetime as dt
import os
import sqlite3

from baza import DB_PATH, SCRIPT_DIR

MAPA = os.environ.get("TRGOVINA_KOPIJE") or os.path.join(SCRIPT_DIR, "sigurnosne_kopije")
ZADRZI = 30


def _mape():
    out = [MAPA]
    dodatna = os.environ.get("TRGOVINA_KOPIJA_MAPA", "").strip()
    if dodatna and dodatna != "-":  # "-" = bez dodatne mape (Kasa_druga_trgovina.cmd)
        out.append(dodatna)
    return out


def napravi(db_path=DB_PATH):
    """Napravi kopiju u svaku mapu. Vraća popis (mapa, putanja ili greška)."""
    ime = "trgovina_%s.db" % dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    rezultat = []
    izvor = sqlite3.connect(db_path)
    try:
        for mapa in _mape():
            try:
                os.makedirs(mapa, exist_ok=True)
                cilj_put = os.path.join(mapa, ime)
                cilj = sqlite3.connect(cilj_put)
                with cilj:
                    izvor.backup(cilj)
                cilj.close()
                _pocisti(mapa)
                rezultat.append({"mapa": mapa, "datoteka": cilj_put, "greska": ""})
            except (OSError, sqlite3.Error) as e:
                rezultat.append({"mapa": mapa, "datoteka": "", "greska": str(e)})
    finally:
        izvor.close()
    return rezultat


def _pocisti(mapa):
    kopije = sorted(f for f in os.listdir(mapa) if f.startswith("trgovina_") and f.endswith(".db"))
    # zadnja kopija svake godine ostaje (ime je trgovina_GGGG-MM-DD_..., pa je sortirano po vremenu)
    godisnje = {f[9:13]: f for f in kopije}
    for f in kopije[:-ZADRZI]:
        if godisnje.get(f[9:13]) == f:
            continue
        try:
            os.remove(os.path.join(mapa, f))
        except OSError:
            pass


def popis():
    out = []
    for mapa in _mape():
        try:
            for f in sorted(os.listdir(mapa), reverse=True):
                if f.startswith("trgovina_") and f.endswith(".db"):
                    st = os.stat(os.path.join(mapa, f))
                    out.append({"mapa": mapa, "ime": f, "velicina": st.st_size,
                                "vrijeme": dt.datetime.fromtimestamp(st.st_mtime)
                                .strftime("%Y-%m-%d %H:%M:%S")})
        except OSError:
            pass
    return out
