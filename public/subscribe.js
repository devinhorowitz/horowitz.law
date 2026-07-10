// subscribe.js -- progressive-enhancement handler for the /subscribe form.
// Lives in an external file (not inline) so it satisfies the site CSP
// (script-src 'self'); inline scripts are blocked unless hash-allowlisted.
(function () {
  var form = document.getElementById('subForm');
  var btn = document.getElementById('subBtn');
  var status = document.getElementById('status');
  if (!form) return;

  function setStatus(msg, kind) {
    status.textContent = msg;
    status.className = 'status' + (kind ? ' ' + kind : '');
  }

  // A Turnstile token is single-use. After every attempt, reset the widget so a
  // retry gets a fresh token instead of replaying a spent one.
  function resetTurnstile() {
    if (window.turnstile && typeof window.turnstile.reset === 'function') {
      try { window.turnstile.reset(); } catch (e) {}
    }
  }

  // 'everything' and the per-area boxes are mutually exclusive as a set:
  // ticking an area unticks everything, ticking everything clears the areas.
  // Plain checkboxes otherwise, so the form still posts sensibly without this.
  var allBox = form.querySelector('input[name="area"][value="all"]');
  var areaBoxes = Array.prototype.slice.call(form.querySelectorAll('input[name="area"]:not([value="all"])'));
  if (allBox) {
    allBox.addEventListener('change', function () {
      if (allBox.checked) areaBoxes.forEach(function (b) { b.checked = false; });
    });
    areaBoxes.forEach(function (b) {
      b.addEventListener('change', function () {
        if (b.checked) allBox.checked = false;
        if (!areaBoxes.some(function (x) { return x.checked; })) allBox.checked = true;
      });
    });
  }

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var email = document.getElementById('email').value.trim();
    var company = document.getElementById('company').value; // honeypot
    // Chosen practice areas. Empty (or 'everything') means the full weekly
    // digest -- the server treats both identically, so legacy posts without
    // the field keep working.
    var areas = areaBoxes.filter(function (b) { return b.checked; })
                         .map(function (b) { return b.value; });
    // State choices, when the multistate group exists (it appears automatically
    // once the jurisdiction registry holds a second state -- see render.py).
    // All states checked, or none rendered, means everything: send [] for both,
    // so today's single-state form posts exactly what it always has.
    var jurisBoxes = Array.prototype.slice.call(form.querySelectorAll('input[name="juris"]'));
    var jurisChecked = jurisBoxes.filter(function (b) { return b.checked; })
                                 .map(function (b) { return b.value; });
    var juris = (jurisBoxes.length && jurisChecked.length < jurisBoxes.length) ? jurisChecked : [];
    if (!email || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
      setStatus('Please enter a valid email address.', 'err');
      return;
    }

    // Cloudflare Turnstile: a solved-challenge token is required. The widget in
    // managed mode usually solves on load, so by submit time this is populated.
    var token = (window.turnstile && typeof window.turnstile.getResponse === 'function')
      ? window.turnstile.getResponse() : '';
    if (!token) {
      setStatus('Please complete the verification, then try again.', 'err');
      return;
    }

    btn.disabled = true;
    setStatus('Subscribing...', '');
    fetch('/api/subscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: email, company: company, turnstileToken: token, areas: areas, juris: juris })
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        var d = res.d || {};
        if (res.ok && d.ok) {
          form.reset();
          resetTurnstile();
          setStatus(d.message || 'Check your inbox to confirm your subscription.', 'ok');
        } else {
          setStatus(d.message || 'Something went wrong. Please try again.', 'err');
          resetTurnstile();
          btn.disabled = false;
        }
      })
      .catch(function () {
        setStatus('Network error. Please try again in a moment.', 'err');
        resetTurnstile();
        btn.disabled = false;
      });
  });
})();
