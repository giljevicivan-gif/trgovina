// Kopija baze izvan računala (kopija_van.py): kartica u panelu Postavke.
// Učitava se iza glavne skripte u kasa.html i koristi njezine pomoćne funkcije (api, esc, toast, MODULI).
(function () {
  const box = document.getElementById('postavke');
  if (!box) return;
  const mb = b => (b / 1048576).toFixed(1).replace('.', ',') + ' MB';

  async function crtaj() {
    const el = document.getElementById('kv-kartica');
    if (!el) return;
    let S;
    try { S = await api('/api/kopija_van'); } catch (e) { el.innerHTML = '<p class="minus">' + esc(e.message) + '</p>'; return; }
    const U = S.upozorenje, Z = S.zadnja_ok;
    el.innerHTML = '<h2 style="margin-top:0">Kopija izvan računala</h2>' +
      '<p class="napomena">Mapa na USB disku (npr. <b>E:\\Trgovina</b>) ili mapa koju sinkronizira OneDrive / Google Drive ' +
      '(npr. <b>%USERPROFILE%\\OneDrive\\Trgovina</b>). Kopija se radi sama jednom dnevno i kod zaključka smjene, pa se provjeri.</p>' +
      '<div class="red" style="flex-wrap:wrap;gap:8px"><label style="flex:1 1 260px">Mapa <input id="kv-mapa" style="width:100%" value="' + esc(S.mapa) + '"' +
      (S.mapa_env ? ' placeholder="' + esc(S.mapa_env) + ' (iz pokreni.bat)"' : ' placeholder="E:\\Trgovina"') + '></label>' +
      '<label>Čuvaj kopija <input id="kv-zadrzi" inputmode="numeric" style="width:60px" value="' + S.zadrzi + '"></label></div>' +
      (S.cilj ? '<p class="napomena">Kopija ide u: <b>' + esc(S.cilj) + '</b> ' + (S.dostupna ? '(dostupna)' : '<span class="minus">(nije dostupna)</span>') + '</p>' : '') +
      '<div class="red"><button class="btn" id="kv-spremi">Spremi mapu</button><button class="btn prim" id="kv-napravi">Napravi kopiju sada</button>' +
      '<button class="btn" id="kv-provjeri">Provjeri</button></div><p id="kv-rez" class="napomena"></p>' +
      (U ? '<p class="' + (U.razina === 'crveno' ? 'minus' : 'upoz') + '"><b>' + esc(U.naslov) + '.</b> ' + esc(U.opis) + '</p>'
         : (Z ? '<p>Zadnja provjerena kopija: <b>' + esc(Z.vrijeme.slice(0, 16)) + '</b> (' + mb(Z.velicina) + ')</p>' : '')) +
      (S.log.length ? '<details><summary>Zadnji pokušaji</summary><table class="t">' + S.log.map(l =>
        '<tr><td>' + esc(l.vrijeme.slice(0, 16)) + '</td><td>' + esc(l.nacin) + '</td><td class="' + (l.ok ? '' : 'minus') + '">' +
        (l.ok ? 'u redu' : 'GREŠKA') + '</td><td style="font-size:12px">' + esc(l.poruka) + '</td></tr>').join('') + '</table></details>' : '');
    const rez = (t, err) => { const r = document.getElementById('kv-rez'); r.textContent = t; r.className = err ? 'minus' : 'napomena'; };
    const spremi = () => api('/api/kopija_van/postavke', {mapa: document.getElementById('kv-mapa').value, zadrzi: document.getElementById('kv-zadrzi').value});
    const gumb = (id, fn) => { const b = document.getElementById(id); b.onclick = async () => {
      b.disabled = true; rez('Radim…');
      let t, err = false;
      try { t = await fn(); } catch (e) { t = e.message; err = true; }
      await crtaj(); rez(t, err);
    }; };
    gumb('kv-spremi', async () => { await spremi(); return 'Spremljeno.'; });
    gumb('kv-napravi', async () => { await spremi(); const r = await api('/api/kopija_van/napravi', {}); return 'Kopija spremljena i provjerena: ' + r.datoteka + ' (' + mb(r.velicina) + ').'; });
    gumb('kv-provjeri', async () => { await spremi(); const r = await api('/api/kopija_van/provjeri', {}); if (!r.ok) throw new Error(r.poruka); return r.poruka; });
  }

  // panel Postavke se crta iznova kod svakog otvaranja: dodaj karticu kad se pojavi
  new MutationObserver(() => {
    if (!MODULI.kopija_van || box.hidden || document.getElementById('kv-kartica')) return;
    const spremi = document.getElementById('p-spremi');
    if (!spremi) return;
    const k = document.createElement('div');
    k.id = 'kv-kartica';
    k.style.cssText = 'border-top:1px solid var(--line);margin-top:12px;padding-top:8px';
    spremi.closest('.red').before(k);
    crtaj();
  }).observe(box, {childList: true, attributes: true, attributeFilter: ['hidden']});
})();
