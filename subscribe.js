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

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var email = document.getElementById('email').value.trim();
    var company = document.getElementById('company').value; // honeypot
    if (!email || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
      setStatus('Please enter a valid email address.', 'err');
      return;
    }
    btn.disabled = true;
    setStatus('Subscribing...', '');
    fetch('/api/subscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: email, company: company })
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        var d = res.d || {};
        if (res.ok && d.ok) {
          form.reset();
          setStatus(d.message || 'Check your inbox to confirm your subscription.', 'ok');
        } else {
          setStatus(d.message || 'Something went wrong. Please try again.', 'err');
          btn.disabled = false;
        }
      })
      .catch(function () {
        setStatus('Network error. Please try again in a moment.', 'err');
        btn.disabled = false;
      });
  });
})();
