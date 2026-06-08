// =============================================================
// horowitz.law - opinions.js
// Page logic for Georgia Appellate Watch: the "new this week"
// signal + per-card NEW badges (computed from each card's
// data-date), and the court / practice-area / new-only filters.
// Theme toggle, keyboard shortcuts, and shared behavior live in
// app.js. DOM is built with createElement/textContent only (no
// innerHTML), to satisfy the site's require-trusted-types-for CSP.
// =============================================================
(function () {
  'use strict';

  var WEEK = 7 * 24 * 60 * 60 * 1000;
  var now = Date.now();
  var cards = Array.prototype.slice.call(document.querySelectorAll('.opinion'));
  if (!cards.length) return;

  // Precompute a lowercased text haystack per card so substring search stays
  // fast as the record grows. Done before the NEW badge is appended so the
  // badge text does not pollute matches. textContent only, never innerHTML, to
  // satisfy the site's require-trusted-types-for CSP.
  cards.forEach(function (c) { c._hay = c.textContent.toLowerCase(); });

  var searchBox = document.getElementById('searchBox');
  var searchCount = document.getElementById('searchCount');
  var query = '';
  var yearBlocks = Array.prototype.slice.call(document.querySelectorAll('.archive-year-block'));
  var yearLinks = Array.prototype.slice.call(document.querySelectorAll('.year-nav a'));

  // --- NEW badges + "new this week" count, from each card's date ---
  var newCount = 0;
  cards.forEach(function (c) {
    var d = new Date(c.getAttribute('data-date') + 'T12:00:00').getTime();
    var isNew = (now - d) <= WEEK && (now - d) >= -WEEK;
    c.classList.toggle('is-new', isNew);
    c.dataset.isnew = isNew ? '1' : '0';
    if (isNew) {
      newCount++;
      var head = c.querySelector('.op-head');
      if (head && !head.querySelector('.badge-new')) {
        var b = document.createElement('span');
        b.className = 'badge-new';
        b.textContent = 'new';
        head.appendChild(b);
      }
    }
  });

  var flag = document.getElementById('newFlag');
  if (flag) {
    if (newCount > 0) {
      flag.classList.add('live');
      var nc = document.getElementById('newCount');
      if (nc) nc.textContent = String(newCount);
    } else {
      flag.classList.add('off');
      var lbl = flag.querySelector('.label');
      if (lbl) lbl.textContent = 'nothing new this week';
    }
  }

  // --- filters: court, practice area, new-only ---
  var courtFilter = 'all', areaFilter = 'all', newOnly = false;

  function setActive(selector, active) {
    document.querySelectorAll(selector).forEach(function (x) {
      x.setAttribute('aria-pressed', x === active ? 'true' : 'false');
    });
  }

  function apply() {
    var tokens = query ? query.split(/\s+/) : [];
    var shown = 0;
    cards.forEach(function (c) {
      var okCourt = courtFilter === 'all' || c.getAttribute('data-court') === courtFilter;
      var areas = (c.getAttribute('data-areas') || '').split(',');
      var okArea = areaFilter === 'all' || areas.indexOf(areaFilter) > -1;
      var okNew = !newOnly || c.dataset.isnew === '1';
      var okText = true;
      for (var i = 0; i < tokens.length; i++) {
        if (c._hay.indexOf(tokens[i]) === -1) { okText = false; break; }
      }
      var visible = okCourt && okArea && okNew && okText;
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

  document.querySelectorAll('[data-court-filter]').forEach(function (ch) {
    ch.addEventListener('click', function () {
      courtFilter = ch.getAttribute('data-court-filter');
      setActive('[data-court-filter]', ch);
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

  var newToggle = document.getElementById('newOnly');
  if (newToggle) {
    newToggle.addEventListener('click', function () {
      newOnly = !newOnly;
      newToggle.setAttribute('aria-pressed', newOnly ? 'true' : 'false');
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
