// Chance.cz CS2 lines, read in a chance.cz tab (Chance refuses plain requests).
//
// Paste into the console of an open https://www.chance.cz/kurzy/esporty-188
// tab. It lists the Counter-Strike competitions, then every match starting
// within 7 days, one request every 2 seconds, and stops on the first answer
// that is not JSON (Chance blocked a browser once after ~100 requests a day).
// When window.__cs.state === 'done', save the round markets:
//
//   copy(JSON.stringify(window.__cs.tables.filter(r =>
//     /^(Handicap \(kola|Počet kol v|Přesný výsledek \d|Vítěz \d\. mapy$|Vítěz \d\. mapy a počet kol)/.test(r[5]))))
//
// into a file and run:  python chance_check.py <file>
// Rows: [match id, start, competition, home, away, table, box, cell, odd].
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const getJSON = async (u, opt) => {
    const r = await fetch(u, Object.assign({credentials: 'include'}, opt || {}));
    if (!(r.headers.get('content-type') || '').includes('json')) throw new Error('not JSON ' + r.status);
    return r.json();
  };
  const C = window.__cs = {comps: [], matches: [], tables: [], state: 'listing'};
  const sports = await getJSON('/rest/offer/v6/sports?fromResults=false&withLive=false&mySelectionWithLiveMatches=false');
  const walk = (x, path) => {
    if (Array.isArray(x)) return x.forEach(v => walk(v, path));
    if (x && typeof x === 'object') {
      const nm = x.title || x.name, p = nm ? path.concat(nm) : path;
      if (x.type === 'COMPETITION' && x.id && /Counter Strike(?! 1v1)/.test(p.join('|'))) C.comps.push({id: x.id, name: nm});
      for (const v of Object.values(x)) if (v && typeof v === 'object') walk(v, p);
    }
  };
  walk(sports, []);
  for (const c of C.comps) {
    const d = await getJSON('/rest/offer/v2/offer?limit=75', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({results: false, highlightAnyTime: false, limit: 75, order: 'DATESTART', type: 'COMPETITION', id: c.id, matchViewFilters: [], withLive: false})});
    const w = x => {
      if (Array.isArray(x)) return x.forEach(w);
      if (x && typeof x === 'object') {
        if (x.id && (x.nameFull || x.name) && x.dateClosed && !x.inLive && String(x.id).length >= 6)
          C.matches.push({id: x.id, name: x.nameFull || x.name, start: x.dateClosed, comp: c.name});
        else for (const v of Object.values(x)) if (v && typeof v === 'object') w(v);
      }
    };
    w(d);
    await sleep(2000);
  }
  const seen = new Set();
  const todo = C.matches.filter(m => new Date(m.start) - Date.now() < 7 * 24 * 3600e3 && !seen.has(m.id) && seen.add(m.id));
  C.todo = todo.length; C.state = 'fetching'; C.fetched = 0;
  for (const m of todo) {
    const mt = (await getJSON('/rest/offer/v3/matches/' + m.id + '?fromResults=false&ticketBuilderId=1')).match;
    C.fetched++;
    const names = ps => (ps || []).map(p => p.name || p.nameFull || p.participantName).join('/');
    for (const t of mt.eventTables || []) for (const b of t.boxes || []) for (const cell of b.cells || [])
      if (cell.active && cell.odd)
        C.tables.push([m.id, m.start, m.comp, names(mt.homeParticipants), names(mt.visitingParticipants),
                       t.name, b.name || '', cell.name || '', cell.odd]);
    await sleep(2000);
  }
  C.state = 'done';
})().catch(e => { window.__cs.state = 'stopped: ' + e.message; });
