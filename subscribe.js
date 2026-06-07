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

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var email = document.getElementById('email').value.trim();
    var company = document.getElementById('company').value; // honeypot
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
      body: JSON.stringify({ email: email, company: company, turnstileToken: token })
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
