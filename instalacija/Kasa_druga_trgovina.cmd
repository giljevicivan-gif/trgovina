@echo off
REM Kasa za DRUGU trgovinu na istom racunalu (npr. Petica), s vlastitom bazom.
REM Kopirajte ovu datoteku na radnu povrsinu i po potrebi promijenite retke "set" ispod.
REM Glavna kasa (ikona Kasa) i njezina baza u C:\TrgovinaPodaci se ne diraju.
REM
REM TRGOVINA_PODACI  mapa s bazom ove trgovine (napravi se sama)
REM TRGOVINA_PORT    mora biti drukciji od glavne kase (ona je obicno 8790)
REM ROBA             "-" = bez uvoza iz Robe; ili putanja do Robe te trgovine, npr. D:\Roba
REM TRGOVINA_NAZIV   naziv na zaslonu i ispisima
set "TRGOVINA_PODACI=C:\TrgovinaPetica"
set "TRGOVINA_PORT=8791"
set "ROBA=-"
set "TRGOVINA_NAZIV=Trgovina PETICA"
set "TRGOVINA_NE_PAMTI=1"
REM "-" = bez dodatne mape za kopije (inace bi se kopije mijesale s glavnom kasom)
set "TRGOVINA_KOPIJA_MAPA=-"
set "TRGOVINA_DB="
set "TRGOVINA_KOPIJE="
set "TRGOVINA_CERT_MAPA="

set "PROG=%LOCALAPPDATA%\Programs\Trgovina"
if not exist "%PROG%\program\pokreni.py" (
  echo Program Trgovina nije instaliran u %PROG%.
  pause
  exit /b 1
)
start "" "%PROG%\python\pythonw.exe" "%PROG%\program\pokreni.py"
