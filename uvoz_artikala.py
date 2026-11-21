# -*- coding: utf-8 -*-
"""
Uvoz artikala iz Robe druge trgovine (npr. kad trgovina prelazi na ovaj program).

Uključuje se u Postavkama (modul "uvoz_artikala"). Za razliku od običnog uvoza
iz Robe (uvoz.py), koji svaki dan usklađuje artikle s Robom ove trgovine, ovo je
jednokratno preuzimanje šifrarnika:

- Robine datoteke se SAMO ČITAJU (mapa godine, mapa Robe ili .zip kopija),
- prvo se napravi PREGLED: koliko je novih, što se preskače i zašto,
- upisuju se SAMO NOVI artikli; postojeći artikli se nikad ne mijenjaju,
- artikl se preskače ako u bazi već postoji ista šifra ili isti barkod,
- prije upisa se napravi sigurnosna kopija baze.

Veza s Robinom šifrom (roba_sifra) se zadržava samo u bazi koja još nema artikle
iz neke Robe i nema zadanu mapu Robe: tada kasnije spajanje Robe te trgovine
osvježava iste artikle umjesto da ih udvostruči. U bazi druge trgovine artikli
ulaze kao naši vlastiti (bez veze), da ih uvoz te druge Robe nikad ne prepiše.
"""

import datetime as dt
import os
import re
import threading
import time
import zipfile

import cijene
import kopija
import postavke
from baza import centi
from dbf import find_file, read_dbf, read_dbf_bytes
from nabava import Greska

IZBORI = {
    "prodani_zaliha": "Prodavani u toj godini ili imaju zalihu",
    "zaliha": "Samo artikli sa zalihom",
    "svi": "Svi artikli",
}
PRIMJERA = 40
_LOCK = threading.Lock()
_CACHE = {}  # (putanja, mtime, velicina) -> pročitani izvor, vrijedi CACHE_SEKUNDI
CACHE_SEKUNDI = 300


# ---------------------------------------------------------------- čitanje izvora

class Izvor:
    """Mapa godine iz Robe, na disku ili u .zip arhivi. Samo čitanje."""

    def __init__(self, putanja):
        self.putanja = putanja
        self.zip = None
        self.mapa = None      # na disku: mapa s artikli.dbf
        self.prefiks = None   # u zipu: "2025/"
        if os.path.isfile(putanja) and putanja.lower().endswith(".zip"):
            try:
                self.zip = zipfile.ZipFile(putanja)
            except (OSError, zipfile.BadZipFile) as e:
                raise Greska("Arhivu %s ne mogu otvoriti: %s" % (putanja, e))
            kandidati = [n for n in self.zip.namelist() if n.lower().rsplit("/", 1)[-1] == "artikli.dbf"]
            if not kandidati:
                raise Greska("U arhivi nema datoteke artikli.dbf (to nije kopija Robe).")
            # najnovija godišnja mapa (npr. 2025/ prije 2024/)
            kandidati.sort(key=lambda n: (max([int(g) for g in re.findall(r"(?:^|/)(\d{4})/", n)] or [0]), n))
            self.prefiks = kandidati[-1][:-len("artikli.dbf")]
            self.opis = "%s (%s)" % (os.path.basename(putanja), self.prefiks.rstrip("/") or "korijen")
            return
        mapa = putanja
        if os.path.isfile(putanja) and os.path.basename(putanja).lower() == "artikli.dbf":
            mapa = os.path.dirname(putanja)
        if not os.path.isdir(mapa):
            raise Greska("Mapa ili datoteka \"%s\" ne postoji." % putanja)
        if not find_file(mapa, "artikli.dbf"):
            godine = sorted(f for f in os.listdir(mapa)
                            if f.isdigit() and len(f) == 4 and find_file(os.path.join(mapa, f), "artikli.dbf"))
            if not godine:
                raise Greska("U mapi %s nema datoteke artikli.dbf ni godišnjih mapa Robe (npr. 2025)." % mapa)
            mapa = os.path.join(mapa, godine[-1])
        self.mapa = mapa
        self.opis = mapa

    def _bajtovi(self, ime):
        if self.zip:
            want = (self.prefiks + ime).lower()
            for n in self.zip.namelist():
                if n.lower() == want:
                    return self.zip.read(n)
            return None
        for m in (self.mapa, os.path.dirname(self.mapa)):
            p = find_file(m, ime)
            if p:
                with open(p, "rb") as f:
                    return f.read()
            if ime.lower().endswith(".dbf"):
                break  # .dbf samo iz mape godine; vulpes.ini može biti i mapu iznad
        return None

    def citaj(self, ime, polja=None):
        b = self._bajtovi(ime)
        return read_dbf_bytes(b, polja) if b else []

    def trgovina(self):
        """Naziv, mjesto i OIB iz vulpes.ini [Report izgled] (lozinke se ne čitaju)."""
        b = self._bajtovi("vulpes.ini")
        if b is None and self.zip and self.prefiks:
            # vulpes.ini u korijenu arhive (iznad mape godine)
            for n in self.zip.namelist():
                if n.lower().rsplit("/", 1)[-1] == "vulpes.ini":
                    b = self.zip.read(n)
                    break
        out = {"naziv": "", "mjesto": "", "oib": ""}
        if not b:
            return out
        sekcija = ""
        for red in b.decode("cp1250", "replace").splitlines():
            red = red.strip()
            if red.startswith("["):
                sekcija = red.strip("[]").lower()
                continue
            if sekcija != "report izgled" or "=" not in red:
                continue
            k, v = red.split("=", 1)
            if k.strip().lower() in out:
                out[k.strip().lower()] = v.strip()
        return out


def _ucitaj(putanja):
    putanja = (putanja or "").strip().strip('"').strip()
    if not putanja:
        raise Greska("Upišite mapu Robe druge trgovine (npr. D:\\Roba\\2025) ili .zip kopiju.")
    try:
        st = os.stat(putanja)
        kljuc = (os.path.abspath(putanja), st.st_mtime, st.st_size)
    except OSError:
        raise Greska("Mapa ili datoteka \"%s\" ne postoji." % putanja)
    if kljuc in _CACHE and time.time() - _CACHE[kljuc]["_vrijeme"] < CACHE_SEKUNDI:
        return _CACHE[kljuc]
    izv = Izvor(putanja)
    stope = {p["SIFRA"]: p["STOPA"] for p in izv.citaj("porez.dbf")}
    artikli = izv.citaj("artikli.dbf")
    if not artikli:
        raise Greska("artikli.dbf je prazan ili se ne može pročitati.")
    prodani = {s.get("ARTIKAL") for s in izv.citaj("mstavke.dbf", {"ARTIKAL", "STATUS"})
               if int(s.get("STATUS") or 0) in (2, 3)}
    podaci = {"opis": izv.opis, "trgovina": izv.trgovina(), "stope": stope,
              "artikli": artikli, "prodani": prodani, "_vrijeme": time.time()}
    if izv.zip:
        izv.zip.close()
    _CACHE.clear()
    _CACHE[kljuc] = podaci
    return podaci


# ---------------------------------------------------------------- priprema

def _jm(jm):
    jm = (jm or "").strip()
    m = jm.lower().replace("0", "o")
    if m in ("kom", "ko", ""):
        return "Kom"
    if m == "kg":
        return "kg"
    return jm[:1].upper() + jm[1:].lower() if jm.isupper() else jm


def _datum(v):
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()[:10]
    return str(v or "")[:10]


def _stanje_baze(con):
    r = con.execute("SELECT COUNT(*), SUM(roba_sifra IS NOT NULL) FROM artikli").fetchone()
    return {"artikala": r[0] or 0, "iz_robe": r[1] or 0,
            "fiskal_oib": postavke.vrijednost(con, "fiskal.oib", ""),
            "fiskal_naziv": postavke.vrijednost(con, "fiskal.naziv_tvrtke", "")}


def _koristeni(con):
    """id artikala na koje se nešto poziva (računi, primke, narudžbe, inventure...): ti se ne brišu."""
    ids = set()
    for (tab,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name <> 'artikli'").fetchall():
        if any(r[1] == "artikl_id" for r in con.execute('PRAGMA table_info("%s")' % tab)):
            ids.update(r[0] for r in con.execute('SELECT DISTINCT artikl_id FROM "%s"' % tab) if r[0] is not None)
    return ids


def _stari(con):
    """Plan za zamjenu: koji se postojeći artikli brišu, a koji samo skrivaju (imaju povijest)."""
    koristeni = _koristeni(con)
    svi = [r[0] for r in con.execute("SELECT id FROM artikli")]
    cuvaj = [i for i in svi if i in koristeni]
    return {"ukupno": len(svi), "brisanje": len(svi) - len(cuvaj), "skrivanje": len(cuvaj), "_cuvaj": cuvaj}


def _plan(con, putanja, izbor, zaliha, roba_mapa, zamijeni=False):
    if izbor not in IZBORI:
        raise Greska("Nepoznat izbor artikala.")
    P = _ucitaj(putanja)
    baza = _stanje_baze(con)
    t = P["trgovina"]

    upozorenja, potvrda = [], False
    if zamijeni:
        baza = dict(baza, iz_robe=0)  # postojeći artikli se miču, pa se ne miješaju
    oib_izvora = re.sub(r"\D", "", t["oib"])
    if baza["fiskal_oib"] and oib_izvora and baza["fiskal_oib"] != oib_izvora:
        upozorenja.append("Ova baza je fiskalizirana na OIB %s, a artikli su iz Robe trgovine s OIB-om %s. "
                          "Vjerojatno je ovo baza druge trgovine." % (baza["fiskal_oib"], oib_izvora))
        potvrda = True
    ista = bool(oib_izvora) and oib_izvora in (baza["fiskal_oib"], postavke.vrijednost(con, "uvoz_artikala.oib", ""))
    if baza["iz_robe"] and not ista:
        upozorenja.append("U ovoj bazi je već %d artikala iz Robe (vaša trgovina). Artikli druge trgovine bi se "
                          "pomiješali s vašima na kasi, u zalihi i izvještajima. Preporuka: za drugu trgovinu "
                          "posebna mapa podataka (vidi UPUTE, Uvoz artikala druge trgovine)." % baza["iz_robe"])
        potvrda = True
    # Robina šifra se zadržava samo u "praznoj" bazi bez zadane Robe (nova instalacija te trgovine)
    veza = (not baza["iz_robe"] or ista) and not roba_mapa
    if zamijeni and con.execute("SELECT 1 FROM roba_stavke LIMIT 1").fetchone() and not ista:
        veza = False  # povijest prodaje iz Robe druge trgovine: Robine šifre se ne bi poklapale

    if zamijeni:
        sifre, barkodi, roba_sifre = set(), set(), set()
    else:
        sifre = {r[0].strip().lower() for r in con.execute("SELECT sifra FROM artikli")}
        barkodi = {r[0].strip() for r in con.execute("SELECT barkod FROM artikli WHERE barkod <> ''")}
        roba_sifre = {r[0] for r in con.execute("SELECT roba_sifra FROM artikli WHERE roba_sifra IS NOT NULL")}

    novi, preskoceni = [], []
    razlozi = {"sifra": 0, "barkod": 0, "bez_cijene": 0, "neaktivan": 0}
    izvan_izbora = 0
    vidjene_sifre, vidjeni_barkodi = set(), set()
    for a in P["artikli"]:
        kol = round(a.get("KOLICINA") or 0.0, 3)
        if izbor == "zaliha" and kol <= 0:
            izvan_izbora += 1
            continue
        if izbor == "prodani_zaliha" and kol <= 0 and a["SIFRA"] not in P["prodani"]:
            izvan_izbora += 1
            continue
        naziv = " ".join((a.get("NAZIV") or "").split())
        sifra = (a.get("MSIFRA") or "").strip() or str(a["SIFRA"])
        barkod = (a.get("BARCODE") or "").strip()
        if not barkod.strip("0"):
            barkod = ""
        stavka = {"sifra": sifra, "barkod": barkod, "naziv": naziv, "cijena": centi(a.get("CIJENA")),
                  "pdv_stopa": P["stope"].get(a.get("PDV"), a.get("PDV_I") or 25.0), "jm": _jm(a.get("JM")),
                  "zaliha": kol}
        razlog = ""
        if a.get("NEAKTIVAN"):
            razlog, k = "neaktivan u Robi", "neaktivan"
        elif not naziv or stavka["cijena"] <= 0:
            razlog, k = "nema naziv ili cijenu", "bez_cijene"
        elif sifra.lower() in sifre or sifra.lower() in vidjene_sifre or (veza and a["SIFRA"] in roba_sifre):
            razlog, k = "šifra %s već postoji" % sifra, "sifra"
        elif barkod and (barkod in barkodi or barkod in vidjeni_barkodi):
            razlog, k = "barkod %s već postoji" % barkod, "barkod"
        if razlog:
            razlozi[k] += 1
            preskoceni.append(dict(stavka, razlog=razlog))
            continue
        vidjene_sifre.add(sifra.lower())
        if barkod:
            vidjeni_barkodi.add(barkod)
        dc, dd = a.get("DCIJENA"), _datum(a.get("DDATUM"))
        stavka.update(roba_sifra=a["SIFRA"] if veza else None, nabavna=centi(a.get("PROS")),
                      kpd=str(a.get("KPD") or "").strip(),
                      sidrena=centi(dc) if dc and dd else 0, sidrena_datum=dd if dc and dd else "")
        novi.append(stavka)

    sa_zalihom = sum(1 for s in novi if s["zaliha"] > 0)
    zadnja = max((a.get("SYSDATUM") for a in P["artikli"] if a.get("SYSDATUM")), default=None)
    return {
        "izvor": P["opis"], "trgovina": t, "baza": baza, "veza": veza,
        "upozorenja": upozorenja, "potvrda_potrebna": potvrda, "ista_trgovina": ista,
        "zadnja_promjena": _datum(zadnja),
        "ukupno_u_robi": len(P["artikli"]), "izvan_izbora": izvan_izbora, "izbor": izbor,
        "izbori": IZBORI, "zaliha": bool(zaliha), "sa_zalihom": sa_zalihom,
        "novi": len(novi), "preskoceni": len(preskoceni), "razlozi": razlozi,
        "primjer_novi": [{k: s[k] for k in ("sifra", "barkod", "naziv", "jm", "cijena", "pdv_stopa", "zaliha")}
                         for s in novi[:PRIMJERA]],
        "primjer_preskoceni": preskoceni[:PRIMJERA],
        "_novi": novi, "zamijeni": bool(zamijeni),
        "stari": _stari(con) if zamijeni else None,
        "roba_mapa": bool(roba_mapa),
    }


def pregled(con, p, roba_mapa=""):
    with _LOCK:
        plan = _plan(con, p.get("putanja"), p.get("izbor") or "prodani_zaliha", p.get("zaliha"), roba_mapa,
                     bool(p.get("zamijeni")))
    plan.pop("_novi")
    if plan["stari"]:
        plan["stari"].pop("_cuvaj")
    return plan


def uvezi(con, p, roba_mapa=""):
    """Upiše samo nove artikle. Postojeće ne dira. Vraća {upisano, kopija}."""
    with _LOCK:
        zamijeni = bool(p.get("zamijeni"))
        plan = _plan(con, p.get("putanja"), p.get("izbor") or "prodani_zaliha", p.get("zaliha"), roba_mapa, zamijeni)
        novi = plan["_novi"]
        if not novi:
            raise Greska("Nema novih artikala za upis.")
        if zamijeni and not p.get("potvrda_brisanja"):
            raise Greska("Potvrdite brisanje svojih artikala (kvačica iznad gumba).")
        if plan["potvrda_potrebna"] and not p.get("potvrda"):
            raise Greska("Potvrdite da artikle stvarno želite u ovu bazu (kvačica ispod upozorenja).")
        kop = [k for k in kopija.napravi() if k["datoteka"]]
        if not kop:
            raise Greska("Sigurnosna kopija baze nije uspjela, uvoz nije napravljen.")
        stupci = {r[1] for r in con.execute("PRAGMA table_info(artikli)")}
        zaliha = bool(p.get("zaliha"))
        nivo = con.isolation_level
        con.isolation_level = ""
        try:
            with con:
                if zamijeni:
                    _makni_stare(con, plan["stari"]["_cuvaj"])
                    # artikli iz vlastite Robe se više ne uvoze, inače bi se stari vratili kod pokretanja
                    con.execute("INSERT OR REPLACE INTO postavke (kljuc, vrijednost) VALUES ('uvoz.bez_artikala', '1')")
                for s in novi:
                    red = {"roba_sifra": s["roba_sifra"], "sifra": s["sifra"], "barkod": s["barkod"],
                           "naziv": s["naziv"], "jm": s["jm"], "cijena": s["cijena"],
                           "pdv_stopa": s["pdv_stopa"], "zaliha": s["zaliha"] if zaliha else 0,
                           "nabavna": s["nabavna"], "aktivan": 1}
                    if "kpd" in stupci:
                        red["kpd"] = s["kpd"]
                    if s["sidrena"] and "sidrena" in stupci:
                        red.update(sidrena=s["sidrena"], sidrena_datum=s["sidrena_datum"])
                        if "roba_sidrena" in stupci:
                            red.update(roba_sidrena=s["sidrena"], roba_sidrena_datum=s["sidrena_datum"],
                                       sidrena_izvor="roba")
                    cur = con.execute("INSERT INTO artikli (%s) VALUES (%s)"
                                      % (", ".join(red), ", ".join("?" * len(red))), list(red.values()))
                    if not s["sidrena"] and "sidrena" in stupci:
                        # sidrena cijena kao za svaki novi artikl (cijene.py), samo za ovaj artikl
                        c, d = cijene.predlozi(con, {"id": cur.lastrowid, "roba_sifra": s["roba_sifra"],
                                                     "cijena": s["cijena"]})
                        con.execute("UPDATE artikli SET sidrena = ?, sidrena_datum = ? WHERE id = ?",
                                    (c, d, cur.lastrowid))
                t = plan["trgovina"]
                opis = "%s artikala druge trgovine: %d novih iz %s%s" % (
                    "zamjena" if zamijeni else "uvoz", len(novi), t["naziv"] or "Robe",
                    " (s zalihom)" if zaliha else "")
                if plan["veza"] and t["oib"]:
                    # ista trgovina kod sljedećeg uvoza (dopuna artikala) ne traži potvrdu
                    con.execute("INSERT OR REPLACE INTO postavke (kljuc, vrijednost) VALUES (?, ?)",
                                ("uvoz_artikala.oib", re.sub(r"\D", "", t["oib"])))
                con.execute("INSERT INTO uvozi (vrijeme, mapa, opis) VALUES (?, ?, ?)",
                            (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), plan["izvor"], opis))
        finally:
            con.isolation_level = nivo
    st = plan["stari"] or {}
    return {"upisano": len(novi), "preskoceno": plan["preskoceni"], "kopija": kop[0]["datoteka"],
            "veza": plan["veza"], "obrisano": st.get("brisanje", 0), "skriveno": st.get("skrivanje", 0)}


def _makni_stare(con, cuvaj):
    """Briše postojeće artikle. Oni s poviješću (računi, primke...) ostaju skriveni, da stari
    dokumenti i dalje imaju naziv: neaktivni, bez barkoda, šifra s oznakom S-, bez veze s Robom."""
    cuvaj = set(cuvaj)
    for (aid,) in con.execute("SELECT id FROM artikli").fetchall():
        if aid in cuvaj:
            con.execute("""UPDATE artikli SET aktivan = 0, roba_sifra = NULL, barkod = '',
                           sifra = CASE WHEN sifra LIKE 'S-%' THEN sifra ELSE 'S-' || sifra END WHERE id = ?""",
                        (aid,))
        else:
            con.execute("DELETE FROM artikli WHERE id = ?", (aid,))


def bez_artikala_iz_robe(con):
    return postavke.vrijednost(con, "uvoz.bez_artikala", "0") == "1"


def vrati_artikle_iz_robe(con):
    postavke.postavi(con, "uvoz.bez_artikala", "0")
