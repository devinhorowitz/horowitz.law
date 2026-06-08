// =============================================================
// horowitz.law - opinions.js
// Page logic for Georgia Appellate Watch: the federal/state court
// filter, the jurisdiction selector, the practice-area filter, and
// the full-text search box. The feed is a rolling two-year window
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
      var okJuris = jurisdictionFilter === 'all' || c.getAttribute('data-jurisdiction') === jurisdictionFilter;
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

  if (searchBox) {
    searchBox.addEventListener('input', function () {
      query = searchBox.value.trim().toLowerCase();
      apply();
    });
    searchBox.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { searchBox.value = ''; query = ''; apply(); }
    });
    try {
      var q0 = new URLSearchParams(location.search).get('q');
      if (q0) { searchBox.value = q0; query = q0.trim().toLowerCase(); }
    } catch (e) {}
  }

  apply();
})();
