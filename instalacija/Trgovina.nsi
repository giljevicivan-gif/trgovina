; Instalacija programa Trgovina (kasa, nabava, zaliha...)
; Gradi se naredbom:  bash build.sh   (Linux, treba makensis)
; Na Windowsima:      makensis /DVERZIJA=0.1.0 Trgovina.nsi  (nakon pripreme mape build, vidi build.sh)
;
; Program ide u   %LOCALAPPDATA%\Programs\Trgovina   (ne treba administrator)
; Podaci idu u    C:\TrgovinaPodaci (moze se promijeniti) i NIKAD se ne brisu
; ni kod nove instalacije ni kod deinstalacije. Certifikati i lozinke se ne ugraduju.

Unicode true
SetCompressor /SOLID lzma
RequestExecutionLevel user

!ifndef VERZIJA
  !define VERZIJA "0.1.0"
!endif
!ifndef BUILD
  !define BUILD "build"
!endif
!define IME "Trgovina"
!define REGKEY "Software\Trgovina"
!define UNINSTKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\Trgovina"

Name "${IME} - kasa (probna ${VERZIJA})"
OutFile "${BUILD}\Trgovina_Setup_${VERZIJA}.exe"
InstallDir "$LOCALAPPDATA\Programs\Trgovina"
InstallDirRegKey HKCU "${REGKEY}" "InstallDir"
BrandingText "${IME} ${VERZIJA}"
VIProductVersion "${VERZIJA}.0"
VIAddVersionKey "ProductName" "${IME}"
VIAddVersionKey "FileDescription" "Instalacija programa ${IME}"
VIAddVersionKey "FileVersion" "${VERZIJA}"
VIAddVersionKey "ProductVersion" "${VERZIJA}"
VIAddVersionKey "LegalCopyright" "Ivan"

!include "MUI2.nsh"
!include "nsDialogs.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"

!define MUI_ICON "kasa.ico"
!define MUI_UNICON "kasa.ico"
!define MUI_ABORTWARNING

Var Roba
Var Naziv
Var Podaci
Var hRoba
Var hNaziv
Var hPodaci
Var Port
Var KopijaMapa

!define MUI_WELCOMEPAGE_TITLE "Instalacija programa Trgovina"
!define MUI_WELCOMEPAGE_TEXT "Ovo instalira nas program za trgovinu (kasa, nabava, zaliha, izvjestaji).$\r$\n$\r$\nOvo je PROBNA verzija. Kasa nije fiskalna dok fiskalizaciju sami ne ukljucite i postavite (test s demo certifikatom).$\r$\n$\r$\nPython i svi potrebni dodaci dolaze s instalacijom, nista drugo ne treba instalirati.$\r$\n$\r$\nAko je kasa otvorena, instalacija ce je zatvoriti. Vasi podaci (baza, kopije, certifikati) se ne diraju."
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
Page custom PostavkePage PostavkeLeave
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "Pokreni kasu sada"
!define MUI_FINISHPAGE_RUN_FUNCTION PokreniKasu
!define MUI_FINISHPAGE_SHOWREADME "$INSTDIR\UPUTE_INSTALACIJA.txt"
!define MUI_FINISHPAGE_SHOWREADME_TEXT "Prikazi kratke upute"
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "Croatian"

Function .onInit
  ; zadane vrijednosti, ili one od prosle instalacije
  ReadRegStr $Roba HKCU "${REGKEY}" "Roba"
  ${If} $Roba == ""
    StrCpy $Roba "C:\Roba"
  ${EndIf}
  ReadRegStr $Naziv HKCU "${REGKEY}" "Naziv"
  ${If} $Naziv == ""
    StrCpy $Naziv "Trgovina"
  ${EndIf}
  ReadRegStr $Podaci HKCU "${REGKEY}" "Podaci"
  ${If} $Podaci == ""
    StrCpy $Podaci "C:\TrgovinaPodaci"
  ${EndIf}
  ; port i dodatnu mapu kopija upisuje kasa pri pokretanju (iz postavke.cmd), da ih Setup ne izbrise
  ReadRegStr $Port HKCU "${REGKEY}" "Port"
  ${If} $Port == ""
    StrCpy $Port "8790"
  ${EndIf}
  ReadRegStr $KopijaMapa HKCU "${REGKEY}" "KopijaMapa"
FunctionEnd

Function PostavkePage
  !insertmacro MUI_HEADER_TEXT "Postavke trgovine" "Kasnije ih mozete promijeniti ponovnim pokretanjem instalacije."
  nsDialogs::Create 1018
  Pop $0

  ${NSD_CreateLabel} 0 0 100% 12u "Mapa programa Roba (u njoj su mape 2025, 2026...):"
  Pop $0
  ${NSD_CreateText} 0 13u 78% 12u "$Roba"
  Pop $hRoba
  ${NSD_CreateBrowseButton} 80% 12u 20% 14u "Odaberi..."
  Pop $0
  ${NSD_OnClick} $0 OdaberiRobu

  ${NSD_CreateLabel} 0 34u 100% 12u "Naziv trgovine (pise se na ispisima):"
  Pop $0
  ${NSD_CreateText} 0 47u 78% 12u "$Naziv"
  Pop $hNaziv

  ${NSD_CreateLabel} 0 68u 100% 12u "Mapa za podatke (baza, sigurnosne kopije, certifikati):"
  Pop $0
  ${NSD_CreateText} 0 81u 78% 12u "$Podaci"
  Pop $hPodaci
  ${NSD_CreateBrowseButton} 80% 80u 20% 14u "Odaberi..."
  Pop $0
  ${NSD_OnClick} $0 OdaberiPodatke

  ${NSD_CreateLabel} 0 102u 100% 36u "Program Robu samo cita i nista u njoj ne mijenja. Mapa za podatke se nikad ne brise, ni kod nadogradnje ni kod deinstalacije. Ne stavljajte je na OneDrive ili mrezni disk."
  Pop $0

  nsDialogs::Show
FunctionEnd

Function OdaberiRobu
  ${NSD_GetText} $hRoba $0
  nsDialogs::SelectFolderDialog "Odaberite mapu programa Roba" "$0"
  Pop $0
  ${If} $0 != error
    ${NSD_SetText} $hRoba "$0"
  ${EndIf}
FunctionEnd

Function OdaberiPodatke
  ${NSD_GetText} $hPodaci $0
  nsDialogs::SelectFolderDialog "Odaberite mapu za podatke" "$0"
  Pop $0
  ${If} $0 != error
    ${NSD_SetText} $hPodaci "$0"
  ${EndIf}
FunctionEnd

Function PostavkeLeave
  ${NSD_GetText} $hRoba $Roba
  ${NSD_GetText} $hNaziv $Naziv
  ${NSD_GetText} $hPodaci $Podaci
  ${If} $Podaci == ""
    MessageBox MB_ICONEXCLAMATION "Upisite mapu za podatke."
    Abort
  ${EndIf}
  ${If} $Naziv == ""
    StrCpy $Naziv "Trgovina"
  ${EndIf}
  ${IfNot} ${FileExists} "$Roba\*.*"
    MessageBox MB_YESNO|MB_ICONQUESTION "Mapa Robe $\"$Roba$\" ne postoji na ovom racunalu.$\r$\n$\r$\nKasa ce raditi, ali bez artikala iz Robe. Nastaviti?" IDYES +2
    Abort
  ${EndIf}
FunctionEnd

Function PokreniKasu
  Exec '"$INSTDIR\python\pythonw.exe" "$INSTDIR\program\pokreni.py"'
FunctionEnd

Section "Program" SecMain
  SectionIn RO

  ; zatvori kasu ako radi iz ove mape (inace se python.exe ne moze zamijeniti)
  nsExec::Exec `powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Process python,pythonw -ErrorAction SilentlyContinue | Where-Object { $$_.Path -like '$INSTDIR\python\*' } | Stop-Process -Force"`
  Pop $0
  Sleep 800

  ; stari program i Python se brisu i stavljaju novi (podaci su u drugoj mapi)
  RMDir /r "$INSTDIR\program"
  RMDir /r "$INSTDIR\python"

  SetOutPath "$INSTDIR\python"
  File /r "${BUILD}\python\*.*"
  SetOutPath "$INSTDIR\program"
  File /r "${BUILD}\program\*.*"
  SetOutPath "$INSTDIR"
  File "${BUILD}\Kasa.cmd"
  File "${BUILD}\UPUTE_INSTALACIJA.txt"
  File "kasa.ico"

  ; postavke za pokretanje (pise se svaki put iz odabira na instalaciji)
  FileOpen $0 "$INSTDIR\postavke.cmd" w
  FileWrite $0 "@echo off$\r$\n"
  FileWrite $0 "REM Ovu datoteku pise instalacija. Mozete je i sami urediti (desni klik, Uredi).$\r$\n"
  FileWrite $0 "set $\"ROBA=$Roba$\"$\r$\n"
  FileWrite $0 "set $\"TRGOVINA_NAZIV=$Naziv$\"$\r$\n"
  FileWrite $0 "set $\"TRGOVINA_PODACI=$Podaci$\"$\r$\n"
  FileWrite $0 "REM Port kase u pregledniku (http://127.0.0.1:PORT). 8766 koristi drugi program.$\r$\n"
  FileWrite $0 "set $\"TRGOVINA_PORT=$Port$\"$\r$\n"
  FileWrite $0 "REM Dodatna mapa za sigurnosne kopije (USB...). Maknite REM i upisite mapu:$\r$\n"
  ${If} $KopijaMapa == ""
    FileWrite $0 "REM set $\"TRGOVINA_KOPIJA_MAPA=D:\Kopije_trgovina$\"$\r$\n"
  ${Else}
    FileWrite $0 "set $\"TRGOVINA_KOPIJA_MAPA=$KopijaMapa$\"$\r$\n"
  ${EndIf}
  FileClose $0

  ; mapa za podatke; ako je prazna, ponudi kopiju postojece baze (stari nacin rada s Radne povrsine)
  CreateDirectory "$Podaci"
  ${IfNot} ${FileExists} "$Podaci\trgovina.db"
    ${If} ${FileExists} "$DESKTOP\roba-pregled\kasa\trgovina.db"
      MessageBox MB_YESNO|MB_ICONQUESTION "Na Radnoj povrsini postoji baza od ranije:$\r$\n$DESKTOP\roba-pregled\kasa\trgovina.db$\r$\n$\r$\nKopirati je u novu mapu podataka ($Podaci)?$\r$\nStara ostaje gdje je.$\r$\n$\r$\nNe = kreni s praznom bazom (artikli se uvezu iz Robe)." /SD IDNO IDNO bez_kopije
        CopyFiles /SILENT "$DESKTOP\roba-pregled\kasa\trgovina.db" "$Podaci\trgovina.db"
        ${If} ${FileExists} "$DESKTOP\roba-pregled\kasa\lanci.db"
          CopyFiles /SILENT "$DESKTOP\roba-pregled\kasa\lanci.db" "$Podaci\lanci.db"
        ${EndIf}
      bez_kopije:
    ${EndIf}
  ${EndIf}

  ; precaci
  ; kasa radi bez crnog prozora (pythonw); "Kasa s prozorom" je za trazenje gresaka
  CreateShortcut "$DESKTOP\Kasa.lnk" "$INSTDIR\python\pythonw.exe" '"$INSTDIR\program\pokreni.py"' "$INSTDIR\kasa.ico" 0 SW_SHOWNORMAL "" "Pokreni kasu"
  CreateDirectory "$SMPROGRAMS\Trgovina"
  CreateShortcut "$SMPROGRAMS\Trgovina\Kasa.lnk" "$INSTDIR\python\pythonw.exe" '"$INSTDIR\program\pokreni.py"' "$INSTDIR\kasa.ico" 0 SW_SHOWNORMAL "" "Pokreni kasu"
  CreateShortcut "$SMPROGRAMS\Trgovina\Zatvori kasu.lnk" "$INSTDIR\python\pythonw.exe" '"$INSTDIR\program\pokreni.py" --zatvori' "$INSTDIR\kasa.ico" 0 SW_SHOWNORMAL "" "Zatvori kasu koja radi u pozadini"
  CreateShortcut "$SMPROGRAMS\Trgovina\Kasa s prozorom (za greske).lnk" "$INSTDIR\Kasa.cmd" "" "$INSTDIR\kasa.ico" 0
  CreateShortcut "$SMPROGRAMS\Trgovina\Mapa s podacima.lnk" "$Podaci"
  CreateShortcut "$SMPROGRAMS\Trgovina\Upute.lnk" "$INSTDIR\program\UPUTE.txt"
  CreateShortcut "$SMPROGRAMS\Trgovina\Deinstaliraj.lnk" "$INSTDIR\Deinstaliraj.exe"

  ; zapamti odabir i upisi u "Aplikacije" za deinstalaciju
  WriteRegStr HKCU "${REGKEY}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${REGKEY}" "Roba" "$Roba"
  WriteRegStr HKCU "${REGKEY}" "Naziv" "$Naziv"
  WriteRegStr HKCU "${REGKEY}" "Podaci" "$Podaci"
  WriteRegStr HKCU "${REGKEY}" "Verzija" "${VERZIJA}"
  WriteUninstaller "$INSTDIR\Deinstaliraj.exe"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayName" "Trgovina (kasa)"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayVersion" "${VERZIJA}"
  WriteRegStr HKCU "${UNINSTKEY}" "Publisher" "Ivan"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayIcon" "$INSTDIR\kasa.ico"
  WriteRegStr HKCU "${UNINSTKEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTKEY}" "UninstallString" "$\"$INSTDIR\Deinstaliraj.exe$\""
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  WriteRegDWORD HKCU "${UNINSTKEY}" "EstimatedSize" $0
SectionEnd

Section "Uninstall"
  nsExec::Exec `powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Process python,pythonw -ErrorAction SilentlyContinue | Where-Object { $$_.Path -like '$INSTDIR\python\*' } | Stop-Process -Force"`
  Pop $0
  Sleep 800
  ReadRegStr $Podaci HKCU "${REGKEY}" "Podaci"
  Delete "$DESKTOP\Kasa.lnk"
  RMDir /r "$SMPROGRAMS\Trgovina"
  RMDir /r "$INSTDIR\program"
  RMDir /r "$INSTDIR\python"
  RMDir /r "$INSTDIR\stara_verzija"
  Delete "$INSTDIR\Kasa.cmd"
  Delete "$INSTDIR\postavke.cmd"
  Delete "$INSTDIR\UPUTE_INSTALACIJA.txt"
  Delete "$INSTDIR\kasa.ico"
  Delete "$INSTDIR\Deinstaliraj.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "${UNINSTKEY}"
  ; REGKEY (zadnji odabir) ostaje radi lakse ponovne instalacije
  MessageBox MB_ICONINFORMATION "Program je uklonjen.$\r$\n$\r$\nVasi podaci NISU obrisani i ostaju u mapi:$\r$\n$Podaci" /SD IDOK
SectionEnd
