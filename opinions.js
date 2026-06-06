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
    var shown = 0;
    cards.forEach(function (c) {
      var okCourt = courtFilter === 'all' || c.getAttribute('data-court') === courtFilter;
      var areas = (c.getAttribute('data-areas') || '').split(',');
      var okArea = areaFilter === 'all' || areas.indexOf(areaFilter) > -1;
      var okNew = !newOnly || c.dataset.isnew === '1';
      var visible = okCourt && okArea && okNew;
      c.hidden = !visible;
      if (visible) shown++;
    });
    var empty = document.getElementById('empty');
    if (empty) empty.hidden = shown > 0;
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

  apply();
})();
