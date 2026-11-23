// eRačun preko posrednika (eracun_posrednik.py): gumb u Nabavi, ulazni eRačuni -> primka ili odbijanje,
// zbirni račun tvrtki kao eRačun, naplata, postavke posrednika.
// Učitava se iza glavne skripte u kasa.html i koristi njezine pomoćne funkcije (api, esc, eur, toast, datumHr...).
(function () {
  const sidro = document.getElementById('n-eracun');
  if (!sidro) return;
  const b = document.createElement('button');
  b.className = 'btn'; b.id = 'n-eposrednik'; b.hidden = true; b.textContent = 'eRačuni (posrednik)';
  b.title = 'Ulazni i izlazni eRačuni preko posrednika (moj-eRačun, ePoslovanje, FINA)';
  sidro.after(b);
  const desno = () => document.getElementById('n-desno');
  const $d = s => desno().querySelector(s);
  const dHr = d => d ? datumHr(String(d).slice(0, 10)) : '';
  const eu = c => c === null || c === undefined ? '' : eur(c);
  const danasIso = () => new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);

  const orig = window.primijeniModule;
  window.primijeniModule = function () { orig(); b.hidden = !MODULI.eracun_posrednik; };
  b.addEventListener('click', () => prikazi());

  let S = null;
  async function prikazi(poruka) {
    try { S = await api('/api/eposrednik'); } catch (e) { toast(e.message, true); return; }
    const P = S.postavke, pos = P.posrednici.find(x => x.kljuc === P.posrednik) || {};
    const mj = new Date(); mj.setDate(0);  // prošli mjesec
    const mjesec = mj.toISOString().slice(0, 7);
    desno().innerHTML =
      `<h2>eRačuni preko posrednika</h2>` +
      `<p class="napomena">Posrednik: <b>${esc(pos.naziv || P.posrednik)}</b>, okolina: ` +
      (P.okolina === 'produkcija' ? '<b class="minus">PRODUKCIJA (pravi eRačuni)</b>' : '<b>test</b>') +
      (pos.provjereno ? '' : ' · <span class="upoz">veza s ovim posrednikom još nije provjerena na njihovoj testnoj okolini</span>') +
      `. Posrednik sam fiskalizira eRačune i javlja Poreznoj odbijanja i naplatu. Kasa se ovime ne mijenja.</p>` +
      (poruka ? `<p class="upoz">${esc(poruka)}</p>` : '') +
      '<h3>Ulazni eRačuni (dobavljači)</h3>' +
      '<div class="red" style="flex-wrap:wrap"><button class="btn prim" id="ep-dohvati">Dohvati nove (30 dana)</button></div>' +
      '<div id="ep-ulazni"></div>' +
      '<h3>Izlazni eRačuni (kupci na tvrtku)</h3>' +
      (S.kupci.length ? '<div class="red" style="flex-wrap:wrap"><select id="ep-kupac">' +
        S.kupci.map(k => `<option value="${k.id}">${esc(k.naziv)} (${esc(k.oib)})</option>`).join('') + '</select>' +
        `<input type="month" id="ep-mj" value="${mjesec}"><button class="btn" id="ep-pregled">Pregled eRačuna</button></div>` +
        '<div id="ep-pregled-box"></div>'
        : '<p class="napomena">Nema kupaca s OIB-om (Narudžbe, Kupci). Zbirni račun se šalje tvrtkama koje plaćaju virmanom.</p>') +
      '<div id="ep-izlazni"></div>' +
      '<details id="ep-post"><summary><b>Postavke posrednika</b></summary><div id="ep-post-box"></div></details>';
    crtajUlazne(); crtajIzlazne(); crtajPostavke();
    $d('#ep-dohvati').onclick = async e => {
      e.target.disabled = true;
      try {
        const r = await api('/api/eposrednik/dohvati', {});
        S.ulazni = r.ulazni; crtajUlazne();
        toast(`Novih eRačuna: ${r.novih}` + (r.greske.length ? '. ' + r.greske.join(' ') : ''), r.greske.length > 0);
      } catch (err) { toast(err.message, true); }
      e.target.disabled = false;
    };
    const pr = $d('#ep-pregled');
    if (pr) pr.onclick = pregledIzlaznog;
    if (!P.korisnik && !P.api_kljuc && !P.cert_putanja) $d('#ep-post').open = true;
  }

  function crtajUlazne() {
    const U = S.ulazni;
    $d('#ep-ulazni').innerHTML = U.length ? '<table class="t"><tr><th>Datum</th><th>Dobavljač</th><th>Broj</th><th class="r">Iznos</th><th>Stanje</th><th></th></tr>' +
      U.map(u => `<tr${u.odbijen ? ' style="color:var(--muted)"' : ''}><td>${dHr(u.datum)}</td><td>${esc(u.naziv)}<br><span class="napomena">${esc(u.oib)}</span></td>` +
        `<td>${esc(u.broj)}</td><td class="r">${eu(u.iznos)}</td><td>${u.odbijen ? '<b>odbijen</b> ' + dHr(u.odbijen) : u.primka ? 'primka ' + esc(u.primka) : esc(u.status)}</td>` +
        `<td class="r" style="white-space:nowrap">` +
        (u.ima_xml && !u.odbijen && !u.primka ? `<button class="btn prim" data-pr="${u.id}" style="padding:2px 8px">U primku</button> ` +
          `<button class="btn" data-od="${u.id}" style="padding:2px 8px">Odbij</button> ` : '') +
        (u.ima_xml ? `<a href="/eposrednik_xml/ulazni/${u.id}" target="_blank">XML</a>` : '<span class="napomena">nije preuzet</span>') +
        `</td></tr><tr hidden id="ep-od-${u.id}"><td colspan="6"></td></tr>`).join('') + '</table>'
      : '<p class="napomena">Nema ulaznih eRačuna. Kliknite Dohvati nove.</p>';
    desno().querySelectorAll('[data-pr]').forEach(x => x.onclick = async () => {
      let E;
      try { E = await api(`/api/eposrednik/ulazni/${x.dataset.pr}/primka`); } catch (e) { return toast(e.message, true); }
      primkaIzERacuna(E);
    });
    desno().querySelectorAll('[data-od]').forEach(x => x.onclick = () => formaOdbij(+x.dataset.od));
  }

  function formaOdbij(id) {
    const tr = $d('#ep-od-' + id); tr.hidden = false;
    tr.firstElementChild.innerHTML = '<div class="red" style="flex-wrap:wrap"><select id="ep-razlog">' +
      S.postavke.razlozi.map(([k, n]) => `<option value="${k}">${esc(n)}</option>`).join('') + '</select>' +
      '<input id="ep-opis" placeholder="Zašto (vidi dobavljač i Porezna)" style="flex:1;min-width:200px">' +
      '<button class="btn prim" id="ep-odbij-da">Odbij eRačun</button><button class="btn" id="ep-odbij-ne">Odustani</button></div>' +
      '<p class="napomena">Odbijanje se ne može poništiti. Posrednik ga javlja Poreznoj (do 20. u sljedećem mjesecu).</p>';
    $d('#ep-odbij-ne').onclick = () => { tr.hidden = true; };
    $d('#ep-odbij-da').onclick = async () => {
      try {
        const r = await api(`/api/eposrednik/ulazni/${id}/odbij`, {vrsta: $d('#ep-razlog').value, opis: $d('#ep-opis').value});
        toast(r.poruka); prikazi();
      } catch (e) { toast(e.message, true); }
    };
  }

  async function pregledIzlaznog() {
    const kid = +$d('#ep-kupac').value, mj = $d('#ep-mj').value, box = $d('#ep-pregled-box');
    let R;
    try { R = await api('/api/eposrednik/izlazni/pregled', {kupac_id: kid, mjesec: mj}); } catch (e) { box.innerHTML = `<p class="minus">${esc(e.message)}</p>`; return; }
    box.innerHTML = `<table class="t"><tr><td>eRačun</td><td><b>${esc(R.oznaka)}</b> od ${dHr(R.datum)}, dospijeće ${dHr(R.dospijece)}</td></tr>` +
      `<tr><td>Kupac</td><td>${esc(R.kupac.naziv)}, OIB ${esc(R.kupac.oib)}</td></tr>` +
      `<tr><td>Narudžbe</td><td>${R.narudzbe.length}</td></tr><tr><td>Ukupno s PDV-om</td><td><b>${eur(R.ukupno)} €</b></td></tr></table>` +
      (R.napomene.length ? `<p class="upoz">${R.napomene.map(esc).join('<br>')}</p>` : '') +
      `<p><button class="btn prim" id="ep-posalji">Pošalji eRačun${R.okolina === 'produkcija' ? '' : ' (TEST)'}</button></p>`;
    $d('#ep-posalji').onclick = async e => {
      if (R.okolina === 'produkcija' && !confirm(`Poslati pravi eRačun ${R.oznaka} kupcu ${R.kupac.naziv} na ${eur(R.ukupno)} €?`)) return;
      e.target.disabled = true;
      try {
        const r = await api('/api/eposrednik/izlazni/posalji', {kupac_id: kid, mjesec: mj});
        toast(r.poruka); prikazi();
      } catch (err) { toast(err.message, true); e.target.disabled = false; prikazi(err.message); }
    };
  }

  function crtajIzlazne() {
    const I = S.izlazni;
    $d('#ep-izlazni').innerHTML = I.length ? '<table class="t"><tr><th>eRačun</th><th>Kupac</th><th class="r">Iznos</th><th>Stanje</th><th>Plaćeno</th><th></th></tr>' +
      I.map(r => `<tr><td>${esc(r.oznaka)}<br><span class="napomena">${dHr(r.datum)}</span></td><td>${esc(r.kupac || '')}<br><span class="napomena">${esc(r.mjesec)}</span></td>` +
        `<td class="r">${eur(r.iznos)}</td><td${r.status === 'greska' ? ' class="minus"' : ''}>${esc(r.status)}${r.poruka ? '<br><span class="napomena">' + esc(r.poruka) + '</span>' : ''}</td>` +
        `<td>${r.placeno_datum ? dHr(r.placeno_datum) + ' · ' + eur(r.placeno_iznos) + ' (' + esc(r.placeno_nacin) + ')' : ''}</td>` +
        '<td class="r" style="white-space:nowrap">' +
        (!r.ext_id ? `<button class="btn" data-izl="${r.id}" data-r="ponovno" style="padding:2px 8px">Pošalji ponovno</button> ` :
          `<button class="btn" data-izl="${r.id}" data-r="osvjezi" style="padding:2px 8px">Osvježi</button> ` +
          (r.placeno_datum ? '' : `<button class="btn" data-plati="${r.id}" style="padding:2px 8px">Plaćeno</button> `)) +
        `<a href="/eposrednik_xml/izlazni/${r.id}" target="_blank">XML</a></td></tr>` +
        `<tr hidden id="ep-pl-${r.id}"><td colspan="6"></td></tr>`).join('') + '</table>' : '';
    desno().querySelectorAll('[data-izl]').forEach(x => x.onclick = async () => {
      try { const r = await api(`/api/eposrednik/izlazni/${x.dataset.izl}/${x.dataset.r}`, {}); toast(r.poruka || r.status || 'U redu'); prikazi(); }
      catch (e) { toast(e.message, true); }
    });
    desno().querySelectorAll('[data-plati]').forEach(x => x.onclick = () => {
      const id = +x.dataset.plati, r = I.find(y => y.id === id), tr = $d('#ep-pl-' + id); tr.hidden = false;
      tr.firstElementChild.innerHTML = `<div class="red" style="flex-wrap:wrap"><label>Datum <input type="date" id="ep-pl-d" value="${danasIso()}" min="${r.datum}" max="${danasIso()}"></label>` +
        `<label>Iznos € <input id="ep-pl-i" inputmode="decimal" style="width:110px" value="${eur(r.iznos)}"></label><select id="ep-pl-n">` +
        S.postavke.nacini.map(([k, n]) => `<option value="${k}">${esc(n)}</option>`).join('') + '</select>' +
        '<button class="btn prim" id="ep-pl-da">Javi naplatu</button></div>';
      $d('#ep-pl-da').onclick = async () => {
        try {
          const o = await api(`/api/eposrednik/izlazni/${id}/placeno`, {datum: $d('#ep-pl-d').value, iznos: $d('#ep-pl-i').value, nacin: $d('#ep-pl-n').value});
          toast(o.poruka); prikazi();
        } catch (e) { toast(e.message, true); }
      };
    });
  }

  function crtajPostavke() {
    const P = S.postavke, box = $d('#ep-post-box');
    const polje = (k, n, tip, pom) => `<label style="display:block;margin:4px 0">${n}<br><input id="ep-p-${k}" type="${tip || 'text'}" style="width:100%;max-width:420px" ` +
      `value="${tip === 'password' ? '' : esc(P[k] || '')}" placeholder="${tip === 'password' && P[k] ? 'upisano (prazno = ne mijenja se)' : esc(pom || '')}"></label>`;
    box.innerHTML =
      '<label>Posrednik <select id="ep-p-posrednik">' + P.posrednici.map(x => `<option value="${x.kljuc}"${x.kljuc === P.posrednik ? ' selected' : ''}>${esc(x.naziv)}</option>`).join('') + '</select></label> ' +
      `<label>Okolina <select id="ep-p-okolina"><option value="test">test</option><option value="produkcija"${P.okolina === 'produkcija' ? ' selected' : ''}>produkcija</option></select></label>` +
      '<div id="ep-p-mer">' + polje('korisnik', 'Korisnik (e-mail ili ID)') + polje('lozinka', 'Lozinka', 'password') +
        polje('software_id', 'SoftwareId (daje moj-eRačun: integracije@moj-eracun.hr)') + polje('company_bu', 'Poslovna jedinica (nije obavezno)') + '</div>' +
      '<div id="ep-p-eposlovanje">' + polje('api_kljuc', 'API ključ (eposlovanje.hr; za probu test.eposlovanje.hr)', 'password') + '</div>' +
      '<div id="ep-p-fina">' + `<p class="napomena">FINA aplikacijski certifikat (.p12; za test demo certifikat): ${esc(P.cert_putanja || 'nije odabran')}</p>` +
        '<input type="file" id="ep-p-cert" accept=".p12,.pfx">' + polje('cert_lozinka', 'Lozinka certifikata', 'password') + polje('erp_id', 'Oznaka programa (ERPID)') + '</div>' +
      polje('url', 'Adresa servisa (FINA je daje kod ugovaranja; za ostale prazno)', 'text', 'https://...') +
      `<p class="napomena">Prodavatelj na eRačunu (iz Fiskalizacije): ${esc(P.prodavatelj.naziv || '—')}, OIB ${esc(P.prodavatelj.oib || '—')}, ${esc(P.prodavatelj.adresa || '—')}</p>` +
      polje('mjesto', 'Mjesto trgovine') + polje('postanski', 'Poštanski broj') +
      polje('prostor', 'Oznaka poslovnog prostora u broju eRačuna', 'text', P.prodavatelj.prostor || '') +
      polje('uredjaj', 'Oznaka naplatnog uređaja za eRačune (drugačija od kase i Robe)') +
      polje('proces', 'Poslovni proces (P1 = isporuka robe i usluga)') +
      '<label id="ep-p-potvrda-l" hidden>Za produkciju upišite PRODUKCIJA <input id="ep-p-potvrda"></label>' +
      '<div class="red" style="flex-wrap:wrap;margin-top:8px"><button class="btn prim" id="ep-p-spremi">Spremi</button><button class="btn" id="ep-p-provjeri">Provjeri vezu</button></div>' +
      (P.zadnja_provjera ? `<p class="napomena">Zadnja uspješna provjera: ${esc(P.zadnja_provjera)}</p>` : '');
    const vidi = () => {
      const v = $d('#ep-p-posrednik').value;
      ['mer', 'eposlovanje', 'fina'].forEach(k => $d('#ep-p-' + k).hidden = k !== v);
      $d('#ep-p-potvrda-l').hidden = !($d('#ep-p-okolina').value === 'produkcija' && P.okolina !== 'produkcija');
    };
    $d('#ep-p-posrednik').onchange = vidi; $d('#ep-p-okolina').onchange = vidi; vidi();
    $d('#ep-p-cert').onchange = async e => {
      const f = e.target.files[0]; if (!f) return;
      const b64 = await new Promise(ok => { const r = new FileReader(); r.onload = () => ok(String(r.result).split(',')[1]); r.readAsDataURL(f); });
      try { await api('/api/eposrednik/certifikat', {ime: f.name, sadrzaj: b64}); toast('Certifikat spremljen'); prikazi(); }
      catch (err) { toast(err.message, true); }
    };
    $d('#ep-p-spremi').onclick = async () => {
      const p = {potvrda: $d('#ep-p-potvrda').value};
      ['posrednik', 'okolina', 'korisnik', 'lozinka', 'software_id', 'company_bu', 'api_kljuc', 'cert_lozinka', 'erp_id', 'url',
       'mjesto', 'postanski', 'prostor', 'uredjaj', 'proces'].forEach(k => { p[k] = $d('#ep-p-' + k).value; });
      try { await api('/api/eposrednik/postavke', p); toast('Spremljeno'); prikazi(); } catch (e) { toast(e.message, true); }
    };
    $d('#ep-p-provjeri').onclick = async () => {
      try { const r = await api('/api/eposrednik/provjeri', {}); prikazi(r.poruka); } catch (e) { prikazi('Provjera nije uspjela: ' + e.message); }
    };
  }
})();
