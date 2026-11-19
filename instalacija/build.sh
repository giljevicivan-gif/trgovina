#!/bin/bash
# Gradi Trgovina_Setup_<verzija>.exe na Linuxu (treba: makensis, unzip, pip, curl).
# Upotreba:  bash build.sh [verzija]
set -euo pipefail
VERZIJA="${1:-}"
PYVER="3.13.17"
OVDJE="$(cd "$(dirname "$0")" && pwd)"
# izvor: mapa ../trgovina (projekt) ili korijen repozitorija (GitHub)
if [ -f "$OVDJE/../trgovina/kasa.py" ]; then IZVOR="$OVDJE/../trgovina"; else IZVOR="$OVDJE/.."; fi
[ -n "$VERZIJA" ] || VERZIJA="$(tr -d ' \r\n' < "$IZVOR/verzija.txt")"
RAD="${RAD:-$(mktemp -d)}"
B="$RAD/build"
rm -rf "$B"; mkdir -p "$B/python" "$B/program" "$RAD/dl"

# 1) Python za Windows (sluzbeni NuGet paket python.org, x64), bez testova i IDLE-a
[ -f "$RAD/dl/python.nupkg" ] || curl -sSL -o "$RAD/dl/python.nupkg" \
  "https://api.nuget.org/v3-flatcontainer/python/$PYVER/python.$PYVER.nupkg"
rm -rf "$RAD/dl/py"; mkdir -p "$RAD/dl/py"; unzip -q "$RAD/dl/python.nupkg" -d "$RAD/dl/py"
cp -r "$RAD/dl/py/tools/." "$B/python/"
rm -rf "$B/python/Lib/test" "$B/python/Lib/idlelib" "$B/python/Lib/tkinter" "$B/python/Lib/turtledemo" \
       "$B/python/Lib/ensurepip" "$B/python/include" "$B/python/libs" "$B/python/tcl" \
       "$B/python/DLLs/_tkinter.pyd" "$B/python/DLLs/tcl"*.dll "$B/python/DLLs/tk"*.dll
find "$B/python" -name "__pycache__" -type d -prune -exec rm -rf {} +

# 2) cryptography (fiskalizacija) za Windows x64, raspakirano u site-packages
mkdir -p "$RAD/dl/whl" "$B/python/Lib/site-packages"
pip download -q --only-binary=:all: --platform win_amd64 --python-version 3.13 --implementation cp \
  -d "$RAD/dl/whl" cryptography
for w in "$RAD"/dl/whl/*.whl; do unzip -qo "$w" -d "$B/python/Lib/site-packages"; done

# 3) nas program (bez slika, baze, certifikata, starih .bat)
cp "$IZVOR"/*.py "$IZVOR"/*.html "$IZVOR"/*.js "$IZVOR"/UPUTE.txt "$B/program/"
echo "$VERZIJA" > "$B/program/verzija.txt"
rm -f "$B/program/"*.db
if ls "$B/program/" | grep -qiE '\.(p12|pfx|db)$'; then echo "GRESKA: certifikat/baza u paketu"; exit 1; fi

# 4) pokretac i upute (Windows CRLF)
for f in Kasa.cmd UPUTE_INSTALACIJA.txt; do sed 's/\r$//; s/$/\r/' "$OVDJE/$f" > "$B/$f"; done
sed -i "s/Setup_0\.1\.0/Setup_$VERZIJA/" "$B/UPUTE_INSTALACIJA.txt"

# 5) instalacija
cd "$OVDJE"
makensis -V2 -DVERZIJA="$VERZIJA" -DBUILD="$B" Trgovina.nsi
echo "Gotovo: $B/Trgovina_Setup_$VERZIJA.exe"
