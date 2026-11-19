"""Nadogradnja programa s GitHuba, bez ponovnog pokretanja Setupa.

Na GitHubu (javni repozitorij, samo kod, bez ikakvih podataka) je u korijenu kod programa i
verzija.txt. "Provjeri nadogradnju" (Postavke) uspoređuje verziju; "Nadogradi" skine kod,
provjeri ga, stare datoteke spremi u mapu stara_verzija, zamijeni ih i ponovno pokrene kasu.
Python, paketi, baza, sigurnosne kopije i certifikati se ne diraju (podaci su u drugoj mapi).
Ako nova verzija ne prođe provjeru, vraća se stara.
"""
import io
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile

MAPA = os.path.dirname(os.path.abspath(__file__))
STARA = os.path.join(os.path.dirname(MAPA), "stara_verzija")
GITHUB = os.environ.get("TRGOVINA_GITHUB") or "giljevicivan-gif/trgovina"
API = os.environ.get("TRGOVINA_NADOGRADNJA_URL") or "https://api.github.com/repos/" + GITHUB  # drugi URL samo za testove
_gh = {"vrijeme": 0, "verzija": None, "greska": ""}


def verzija():
    try:
        with open(os.path.join(MAPA, "verzija.txt"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "?"


POKRENUTA = verzija()  # verzija koja sada radi (datoteka se kod nadogradnje mijenja prije ponovnog pokretanja)


def kljuc(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


def dozvoljena(ime):
    """Samo datoteke programa u korijenu (nikad baza, certifikat, slike ni postavke)."""
    return bool(re.fullmatch(r"[A-Za-z0-9_\-]+\.(py|html|js)", ime)) or ime in ("verzija.txt", "UPUTE.txt")


def _dohvati(url, timeout=15, zaglavlja=None):
    z = {"User-Agent": "trgovina-kasa", "Cache-Control": "no-cache"}
    z.update(zaglavlja or {})
    with urllib.request.urlopen(urllib.request.Request(url, headers=z), timeout=timeout) as r:
        return r.read()


def github_verzija(odmah=False):
    """Verzija na GitHubu (najviše jednom u 5 minuta; bez interneta None)."""
    if odmah or time.time() - _gh["vrijeme"] > 300:
        _gh["vrijeme"] = time.time()
        try:
            try:  # API daje svježu datoteku (raw.githubusercontent zna kasniti)
                v = _dohvati(API + "/contents/verzija.txt", 10,
                             {"Accept": "application/vnd.github.raw"})
            except Exception:
                v = _dohvati("https://raw.githubusercontent.com/%s/main/verzija.txt" % GITHUB, 10)
            _gh["verzija"], _gh["greska"] = v.decode("utf-8").strip()[:40], ""
        except Exception as e:
            _gh["verzija"] = None
            _gh["greska"] = ("Na GitHubu još nema programa." if "404" in str(e)
                             else "Nema veze s GitHubom (%s)." % e)
    return _gh["verzija"]


def stanje(odmah=False):
    na_gh = github_verzija(odmah)
    return {"verzija": POKRENUTA, "github": na_gh, "greska": _gh["greska"],
            "novija": bool(na_gh) and kljuc(na_gh) > kljuc(POKRENUTA)}


def procitaj_zip(sadrzaj):
    """Datoteke programa iz zipa s GitHuba (mapa "trgovina-main/" se makne); provjera ispravnosti."""
    z = zipfile.ZipFile(io.BytesIO(sadrzaj))
    datoteke = {}
    for i in z.infolist():
        ime = i.filename.split("/", 1)[-1]
        if not i.is_dir() and "/" not in ime and dozvoljena(ime):
            datoteke[ime] = z.read(i.filename)
    if "verzija.txt" not in datoteke or "kasa.py" not in datoteke or "pokreni.py" not in datoteke:
        raise ValueError("Na GitHubu nije ispravan program (nema verzija.txt, kasa.py ili pokreni.py).")
    for ime, d in datoteke.items():
        if ime.endswith(".py"):
            try:
                compile(d, ime, "exec")
            except SyntaxError as e:
                raise ValueError("Nova verzija je oštećena (%s: %s)." % (ime, e))
    return datoteke["verzija.txt"].decode("utf-8").strip(), datoteke


def _python(bez_prozora=True):
    exe = sys.executable
    if os.name == "nt" and bez_prozora and exe.lower().endswith("python.exe"):
        pyw = exe[:-10] + "pythonw.exe"
        if os.path.exists(pyw):
            return pyw
    return exe


def _bez_prozora():
    return 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW


def _provjeri_novu():
    """Može li se nova verzija učitati (svi moduli)?"""
    r = subprocess.run([_python(False), "-c", "import kasa, pokreni, nadogradnja; print('ok')"], cwd=MAPA,
                       capture_output=True, text=True, timeout=120, creationflags=_bez_prozora())
    if r.returncode != 0 or "ok" not in r.stdout:
        raise ValueError("Nova verzija se ne pokreće: " + (r.stderr or r.stdout).strip()[-400:])


def instaliraj(sadrzaj):
    """Zamijeni datoteke programa; ako nova verzija ne prođe provjeru, vrati staru."""
    nova, datoteke = procitaj_zip(sadrzaj)
    if kljuc(nova) <= kljuc(verzija()):
        raise ValueError("Na GitHubu nije novija verzija (%s)." % nova)
    shutil.rmtree(STARA, ignore_errors=True)
    os.makedirs(STARA, exist_ok=True)
    for ime in os.listdir(MAPA):
        if os.path.isfile(os.path.join(MAPA, ime)):
            shutil.copy2(os.path.join(MAPA, ime), os.path.join(STARA, ime))
    stare = [ime for ime in os.listdir(MAPA)  # datoteke koje nova verzija više nema (STARA ih čuva)
             if ime not in datoteke and ime.endswith((".py", ".html", ".js"))
             and os.path.isfile(os.path.join(MAPA, ime))]
    try:
        for ime in stare:
            os.remove(os.path.join(MAPA, ime))
        for ime, d in datoteke.items():
            privremena = os.path.join(MAPA, ime + ".novo")
            with open(privremena, "wb") as f:
                f.write(d)
            os.replace(privremena, os.path.join(MAPA, ime))
        shutil.rmtree(os.path.join(MAPA, "__pycache__"), ignore_errors=True)
        _provjeri_novu()
    except Exception:
        neuspjeh = []
        for ime in list(datoteke) + stare:  # vrati staru verziju (svaku datoteku posebno, s ponavljanjem)
            put = os.path.join(MAPA, ime)
            for pokusaj in range(5):
                try:
                    if os.path.exists(os.path.join(STARA, ime)):
                        shutil.copy2(os.path.join(STARA, ime), put)
                    elif os.path.exists(put):
                        os.remove(put)
                    break
                except OSError:
                    time.sleep(0.5)
            else:
                neuspjeh.append(ime)
        if neuspjeh:
            raise ValueError("Nadogradnja nije uspjela, a stara verzija se nije mogla do kraja vratiti (%s). "
                             "Pokrenite Setup ponovno." % ", ".join(neuspjeh))
        raise
    return nova


def ponovno_pokreni():
    """Pokreni novu verziju (ona pričeka da se ova ugasi), pa ugasi ovu."""
    flags = _bez_prozora() | (0x00000008 if os.name == "nt" else 0)  # DETACHED_PROCESS
    subprocess.Popen([_python(), os.path.join(MAPA, "pokreni.py"), "--nakon-nadogradnje"],
                     cwd=MAPA, creationflags=flags, close_fds=True)
    threading.Timer(1.5, lambda: os._exit(0)).start()


_nadogradnja_traje = threading.Lock()


def nadogradi():
    if not _nadogradnja_traje.acquire(blocking=False):
        raise ValueError("Nadogradnja je već u tijeku, pričekajte.")
    try:
        return _nadogradi()
    except Exception:
        _nadogradnja_traje.release()
        raise  # nakon uspjeha brava ostaje zaključana dok se kasa ne ugasi


def _nadogradi():
    if not os.path.exists(os.path.join(MAPA, "pokreni.py")) or not os.path.isdir(os.path.join(os.path.dirname(MAPA), "python")):
        raise ValueError("Nadogradnja radi samo u instaliranoj verziji (Setup).")
    sadrzaj = _dohvati(API + "/zipball/main", 120)
    nova = instaliraj(sadrzaj)
    _gh["vrijeme"] = 0
    ponovno_pokreni()
    return {"ok": True, "verzija": nova}
