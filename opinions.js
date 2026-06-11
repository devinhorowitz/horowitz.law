// =============================================================
// horowitz.law - opinions.js
// Page logic for Georgia Appellate Watch: the federal/state court
// filter, the jurisdiction selector, the practice-area filter, the
// full-text search box (all four mirrored into the URL so a filtered
// view is a shareable link), and the copy-citation buttons. The feed is a rolling two-year window
// arranged newest-first, so there is no "new"-recency signal here.
// Theme toggle, keyboard shortcuts, and shared behavior live in
// app.js. The DOM is read with textContent only (never innerHTML),
// to satisfy the site's require-trusted-types-for CSP.
// =============================================================
(function () {
  'use strict';

  var cards = Array.prototype.slice.call(document.querySelectorAll('.opinion'));
  if (!cards.length) return;

  // Precompute a lowercased text haystack per card so substring search stays
  // fast as the record grows. textContent only, never innerHTML, to satisfy the
  // site's require-trusted-types-for CSP.
  cards.forEach(function (c) { c._hay = c.textContent.toLowerCase(); });

  var searchBox = document.getElementById('searchBox');
  var searchCount = document.getElementById('searchCount');
  var query = '';
  var yearBlocks = Array.prototype.slice.call(document.querySelectorAll('.archive-year-block'));
  var yearLinks = Array.prototype.slice.call(document.querySelectorAll('.year-nav a'));

  // --- filters: court system (state/federal), jurisdiction, practice area ---
  // The jurisdiction selector defaults to its selected option (Georgia today);
  // with one option this is a no-op until more states are added, but it is wired
  // so it just works when they are. Pages without the selector show all.
  var jSelect = document.getElementById('jurisdictionSelect');
  var systemFilter = 'all';
  var jurisdictionFilter = jSelect ? jSelect.value : 'all';
  var areaFilter = 'all';
  var jurisDefault = jurisdictionFilter;   // the page's own default; only a departure goes in the URL
  var rawQuery = '';                       // original-case search text, for a readable ?q=

  // Mirror the live filter state into the URL (replaceState: filtering is one
  // view, not a history trail). Defaults are omitted so an unfiltered page has
  // a bare URL; params this script does not own are preserved; the #op- anchor
  // survives. A filtered view of the feed is thereby a sendable link.
  function syncURL() {
    try {
      var p = new URLSearchParams(location.search);
      ['q', 'court', 'area', 'juris'].forEach(function (k) { p.delete(k); });
      if (rawQuery) p.set('q', rawQuery);
      if (systemFilter !== 'all') p.set('court', systemFilter);
      if (areaFilter !== 'all') p.set('area', areaFilter);
      if (jurisdictionFilter !== jurisDefault) p.set('juris', jurisdictionFilter);
      var qs = p.toString();
      history.replaceState(null, '', location.pathname + (qs ? '?' + qs : '') + location.hash);
    } catch (e) {}
  }

  function setActive(selector, active) {
    document.querySelectorAll(selector).forEach(function (x) {
      x.setAttribute('aria-pressed', x === active ? 'true' : 'false');
    });
  }

  function apply() {
    var tokens = query ? query.split(/\s+/) : [];
    var shown = 0;
    cards.forEach(function (c) {
      var okSystem = systemFilter === 'all' || c.getAttribute('data-system') === systemFilter;
      // data-jurisdiction may carry a comma-list once federal cards are stamped
      // for every covered state they bind; membership here, so a single value
      // behaves exactly as it always has.
      var okJuris = jurisdictionFilter === 'all' ||
        (',' + (c.getAttribute('data-jurisdiction') || '') + ',').indexOf(',' + jurisdictionFilter + ',') > -1;
      var areas = (c.getAttribute('data-areas') || '').split(',');
      var okArea = areaFilter === 'all' || areas.indexOf(areaFilter) > -1;
      var okText = true;
      for (var i = 0; i < tokens.length; i++) {
        if (c._hay.indexOf(tokens[i]) === -1) { okText = false; break; }
      }
      var visible = okSystem && okJuris && okArea && okText;
      c.hidden = !visible;
      if (visible) shown++;
    });

    // On the archive, cards are grouped into year sections with a jump nav;
    // hide any section (and its nav link) whose cards are now all hidden.
    yearBlocks.forEach(function (sec) {
      sec.hidden = !sec.querySelector('.opinion:not([hidden])');
    });
    yearLinks.forEach(function (a) {
      var id = (a.getAttribute('href') || '').slice(1);
      var sec = id && document.getElementById(id);
      var block = sec && sec.closest && sec.closest('.archive-year-block');
      if (block) a.style.display = block.hidden ? 'none' : '';
    });

    var empty = document.getElementById('empty');
    if (empty) empty.hidden = shown > 0;
    if (searchCount) searchCount.textContent = query ? (shown + ' of ' + cards.length) : '';
    syncURL();
  }

  document.querySelectorAll('[data-system-filter]').forEach(function (ch) {
    ch.addEventListener('click', function () {
      systemFilter = ch.getAttribute('data-system-filter');
      setActive('[data-system-filter]', ch);
      apply();
    });
  });

  document.querySelectorAll('[data-area-filter]').forEach(function (ch) {
    ch.addEventListener('click', function () {
      areaFilter = ch.getAttribute('data-area-filter');
      setActive('[data-area-filter]', ch);
      apply();
    });
  });

  if (jSelect) {
    jSelect.addEventListener('change', function () {
      jurisdictionFilter = jSelect.value;
      apply();
    });
  }

  // --- restore filter state from the URL (the read half of syncURL). Values
  // are matched against the controls that actually exist, so an unknown or
  // stale param is ignored rather than wedging the page. ---
  try {
    var p0 = new URLSearchParams(location.search);
    var token = /^[\w-]+$/;
    var c0 = p0.get('court');
    if (c0 && token.test(c0)) {
      var cBtn = document.querySelector('[data-system-filter="' + c0 + '"]');
      if (cBtn) { systemFilter = c0; setActive('[data-system-filter]', cBtn); }
    }
    var a0 = p0.get('area');
    if (a0 && token.test(a0)) {
      var aBtn = document.querySelector('[data-area-filter="' + a0 + '"]');
      if (aBtn) { areaFilter = a0; setActive('[data-area-filter]', aBtn); }
    }
    var j0 = p0.get('juris');
    if (j0 && token.test(j0)) {
      jurisdictionFilter = j0;
      if (jSelect) jSelect.value = j0;
    }
  } catch (e) {}

  if (searchBox) {
    // Debounce: apply() walks every card and year block, so re-filtering on every
    // keystroke would jank on mobile once the archive holds a few hundred cards.
    // 80ms is below perception for typing but coalesces a burst into one pass.
    var applyTimer = null;
    function scheduleApply() {
      if (applyTimer) clearTimeout(applyTimer);
      applyTimer = setTimeout(function () { applyTimer = null; apply(); }, 80);
    }
    searchBox.addEventListener('input', function () {
      rawQuery = searchBox.value.trim();
      query = rawQuery.toLowerCase();
      scheduleApply();
    });
    searchBox.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        searchBox.value = ''; query = ''; rawQuery = '';
        if (applyTimer) { clearTimeout(applyTimer); applyTimer = null; }
        apply();   // clearing should feel instant
      }
    });
    try {
      var q0 = new URLSearchParams(location.search).get('q');
      if (q0) { searchBox.value = q0; rawQuery = q0.trim(); query = rawQuery.toLowerCase(); }
    } catch (e) {}
  }

  // --- copy-citation buttons. The markup is server-rendered on every card;
  // the buttons stay hidden until the async Clipboard API is confirmed here
  // (base.css gates them behind html.can-copy), so a no-JS or legacy reader
  // never sees a dead control. textContent only, per the CSP. ---
  if (navigator.clipboard && navigator.clipboard.writeText) {
    document.documentElement.classList.add('can-copy');
    document.addEventListener('click', function (e) {
      var btn = e.target && e.target.closest && e.target.closest('.op-copycite');
      if (!btn) return;
      navigator.clipboard.writeText(btn.getAttribute('data-cite') || '').then(function () {
        btn.textContent = '[ copied ]';
        btn.classList.add('copied');
        setTimeout(function () { btn.textContent = '[ copy cite ]'; btn.classList.remove('copied'); }, 1400);
      }, function () {
        btn.textContent = '[ copy failed ]';
        setTimeout(function () { btn.textContent = '[ copy cite ]'; }, 1400);
      });
    });
  }

  apply();
})();
