// pickem/capture.js
// =================
// Capture Underdog's UFC pick'em board as one pipe-delimited string.
//
// Run in the browser console (or via Claude in Chrome's javascript tool) on
//   https://app.underdogsports.com/pick-em/higher-lower/all/MMA
// while logged in. It clicks through each stat tab, reads the props, and
// copies the result to the clipboard (and returns it). Save it to a file and
// archive it with:
//   ./.venv/bin/python -m pickem.archive add board.txt --card "UFC 333" --date 2026-10-24
//
// Row format:  STAT|Fighter|line|higher_pct|lower_pct   (rows joined by ';')
// STAT: ML (fight-pick "to win" %), SS, FT, TD, KO, SUB, FIN.
// An empty pct means Underdog only offers that side.
(async () => {
  const rows = [];
  const text = () => document.body.innerText;
  const lines = (t) => t.split('\n').map((s) => s.trim()).filter(Boolean);

  // Fight picks: "<A full>\n<B full>\nSat, 1:20pm ... A. X\nTo win\n51% ... B. Y\nTo win\n51%"
  {
    const L = lines(text());
    for (let i = 2; i < L.length; i++) {
      if (!/^(Mon|Tue|Wed|Thu|Fri|Sat|Sun), /.test(L[i])) continue;
      const pair = [L[i - 2], L[i - 1]];
      const pcts = [];
      for (let j = i + 1; j < Math.min(L.length, i + 20) && pcts.length < 2; j++) {
        if (L[j] === 'To win' && /%$/.test(L[j + 1] || '')) pcts.push(L[j + 1].replace('%', ''));
      }
      if (pcts.length === 2) {
        rows.push(['ML', pair[0], '0.5', pcts[0], ''].join('|'));
        rows.push(['ML', pair[1], '0.5', pcts[1], ''].join('|'));
      }
    }
  }

  const TABS = {
    'Significant Strikes': 'SS', 'Fight Time (Mins)': 'FT', 'Takedowns': 'TD',
    'Knockouts': 'KO', 'Submissions': 'SUB', 'Finishes': 'FIN',
  };
  for (const [label, code] of Object.entries(TABS)) {
    const btn = [...document.querySelectorAll('button,a,div[role=button],span')]
      .find((e) => e.textContent.trim() === label && e.offsetParent);
    if (!btn) continue;
    btn.click();
    await new Promise((r) => setTimeout(r, 2500));
    const t = text();
    const L = lines(t.slice(t.indexOf('Round Finish') + 12, t.indexOf('Add picks')));
    for (let i = 1; i < L.length; i++) {
      if (!(/ - (Mon|Tue|Wed|Thu|Fri|Sat|Sun) /.test(L[i]) && /^[\d.]+$/.test(L[i + 1] || ''))) continue;
      let hi = '', lo = '';
      for (let j = i + 3; j < i + 8 && j < L.length; j++) {
        if (L[j] === 'Higher') hi = (L[j + 1] || '').replace('%', '');
        if (L[j] === 'Lower') lo = (L[j + 1] || '').replace('%', '');
        if (/ - (Mon|Tue|Wed|Thu|Fri|Sat|Sun) /.test(L[j])) break;
      }
      rows.push([code, L[i - 1], L[i + 1], hi, lo].join('|'));
    }
  }
  const board = rows.join(';');
  try { await navigator.clipboard.writeText(board); } catch (e) { /* not focused */ }
  window.__underdogBoard = board;
  return `${rows.length} props captured (window.__underdogBoard)`;
})();
