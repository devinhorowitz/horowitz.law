// =============================================================
// horowitz.law — app.js
// Single shared script for both index.html and resume.html.
// Feature-detects: only the bits relevant to the current page run.
// =============================================================

(function () {
  'use strict';

  // -----------------------------------------------------------
  // Console greeting — for the curious who open DevTools.
  // Hidden from regular visitors. Logs once per page load.
  // -----------------------------------------------------------
  try {
    console.log(
      '%c~ horowitz.law',
      'color: #ff9e5e; font-family: ui-monospace, monospace; font-size: 14px; font-weight: 600; padding: 4px 0;'
    );
    console.log(
      '%chand-coded · vanilla everything · no tracking\nsource: https://github.com/devinhorowitz/horowitz.law\n\nif you\'re reading this, hi.',
      'color: #807a72; font-family: ui-monospace, monospace; font-size: 12px; line-height: 1.6;'
    );
  } catch (e) { /* noop */ }


  // -----------------------------------------------------------
  // Hero name typing animation — types out the h1 name
  // character-by-character like a human at a console.
  // Falls back gracefully:
  //   - No JS / script fails: full name shown in HTML markup
  //   - prefers-reduced-motion: skip animation, show full name
  //   - Screen readers: get the full name via aria-label
  //   - User prints mid-animation: finalize text immediately
  // -----------------------------------------------------------
  (function typeHeroName() {
    const cursor = document.querySelector('h1 .cursor');
    if (!cursor) return;
    const h1 = cursor.parentElement;
    const textNode = h1.firstChild;
    if (!textNode || textNode.nodeType !== Node.TEXT_NODE) return;

    if (window.matchMedia &&
        window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      return;
    }

    const fullText = textNode.textContent;
    h1.setAttribute('aria-label', fullText);
    textNode.textContent = '';

    let i = 0;
    let finished = false;
    function typeNext() {
      if (finished || i >= fullText.length) {
        finished = true;
        return;
      }
      const ch = fullText[i++];
      textNode.textContent += ch;

      // Base timing: 55-145ms — humans don't hit keys at perfect intervals
      let delay = 55 + Math.random() * 90;

      // Micro-pause (~12% chance): brief hesitation, "what's the next letter"
      if (Math.random() < 0.12) {
        delay += 50 + Math.random() * 100;
      }

      // Thinking beat (~3% chance): longer pause, like brain caught on something
      if (Math.random() < 0.03) {
        delay += 180 + Math.random() * 220;
      }

      // Word boundary: spaces get a natural inter-word pause
      if (ch === ' ') delay += 90 + Math.random() * 90;

      // Period: longest natural pause, like brain registers punctuation
      if (ch === '.') delay += 140 + Math.random() * 160;

      setTimeout(typeNext, delay);
    }

    // Snap to full text if the user invokes print mid-animation
    window.addEventListener('beforeprint', function () {
      if (!finished) {
        textNode.textContent = fullText;
        finished = true;
      }
    });

    // Initial pause is also randomized — each load starts a beat differently
    setTimeout(typeNext, 300 + Math.random() * 300);
  })();

  // -----------------------------------------------------------
  // Theme toggle (both pages)
  // -----------------------------------------------------------
  const toggle = document.getElementById('themeToggle');
  const root = document.documentElement;

  function getSaved() {
    try { return localStorage.getItem('horowitz-theme'); } catch (e) { return null; }
  }
  function setSaved(v) {
    try { localStorage.setItem('horowitz-theme', v); } catch (e) { /* noop */ }
  }

  // -----------------------------------------------------------
  // Shared interaction feedback. The synthesized "tactile click"
  // is a deliberate device-control gesture, used by the theme
  // toggle and the print button. Hoisted to the top level so any
  // handler can trigger it. Navigation links intentionally stay
  // silent — the click maps to "pressing a control," not "going
  // somewhere," which keeps the analog conceit coherent (and a
  // link's sound would get cut off by same-tab page unload anyway).
  // Respects prefers-reduced-motion.
  // -----------------------------------------------------------
  const reduceMotion = window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  let audioCtx = null;
  function playClickSound() {
    if (reduceMotion) return;
    try {
      if (!audioCtx) {
        const Ctx = window.AudioContext || window.webkitAudioContext;
        if (!Ctx) return;
        audioCtx = new Ctx();
      }
      // iOS Safari can leave the context suspended (tab backgrounding,
      // audio-session interruptions); resume it within this user gesture
      // so the click actually plays instead of failing silently.
      if (audioCtx.state === 'suspended' && audioCtx.resume) {
        audioCtx.resume();
      }
      const now = audioCtx.currentTime;
      const duration = 0.035 + Math.random() * 0.025; // 35-60ms

      // Brief noise burst, naturally decaying inside the buffer
      const buf = audioCtx.createBuffer(1, Math.floor(audioCtx.sampleRate * duration), audioCtx.sampleRate);
      const data = buf.getChannelData(0);
      for (let i = 0; i < data.length; i++) {
        data[i] = (Math.random() * 2 - 1) * (1 - i / data.length);
      }
      const source = audioCtx.createBufferSource();
      source.buffer = buf;

      // Bandpass filter centers the noise into "click" frequency band
      const filter = audioCtx.createBiquadFilter();
      filter.type = 'bandpass';
      filter.frequency.value = 1200 + Math.random() * 600; // 1200-1800 Hz, slight variance
      filter.Q.value = 1.4;

      // Gain envelope: instant attack, fast exponential decay
      const gain = audioCtx.createGain();
      const vol = 0.08 + Math.random() * 0.04; // 0.08-0.12
      gain.gain.setValueAtTime(vol, now);
      gain.gain.exponentialRampToValueAtTime(0.001, now + duration);

      source.connect(filter);
      filter.connect(gain);
      gain.connect(audioCtx.destination);

      source.start(now);
      source.stop(now + duration);
    } catch (e) {
      // Silently fail if Web Audio is blocked or unavailable
    }
  }

  // Brief haptic pulse on supported devices (Android browsers). Double-pulse
  // pattern mimics the click of a physical control. No-ops on iOS (Apple
  // doesn't implement Web Vibration) and on desktop (no vibration hardware).
  // Hoisted to top level so the toggle and save button share it. Respects
  // prefers-reduced-motion.
  function triggerHaptic() {
    if (reduceMotion) return;
    try {
      if ('vibrate' in navigator) {
        navigator.vibrate([8, 12, 8]);
      }
    } catch (e) {
      // silently fail if blocked or unsupported
    }
  }

  if (toggle) {
    const mq = window.matchMedia ? window.matchMedia('(prefers-color-scheme: light)') : null;

    function applyTheme(isLight) {
      if (isLight) {
        root.setAttribute('data-theme', 'light');
        toggle.textContent = '[ dark ]';
        toggle.setAttribute('aria-pressed', 'true');
      } else {
        // Set 'dark' explicitly rather than removing the attribute: the
        // first-visit media query keys off :not([data-theme="dark"]), so an
        // explicit dark choice must mark itself to override a light-mode OS.
        root.setAttribute('data-theme', 'dark');
        toggle.textContent = '[ light ]';
        toggle.setAttribute('aria-pressed', 'false');
      }
    }

    // Brief analog-feel flicker on theme transitions. Duration is randomized
    // (450-700ms) so each transition has its own slight cadence — same spirit
    // as the typing animation. Skipped under prefers-reduced-motion.
    function flickerTransition() {
      if (reduceMotion) return;
      const duration = 450 + Math.random() * 250;
      root.style.setProperty('--flicker-duration', duration + 'ms');
      root.classList.add('theme-flickering');
      setTimeout(function () {
        root.classList.remove('theme-flickering');
      }, duration + 30);
    }

    // First-visit precedence: explicit user choice > OS preference > dark
    // default. For unsaved visitors the CSS media query has already painted
    // the OS-preferred theme (flash-free); JS only needs to set the button
    // label and, for OS-light, mark data-theme so the toggle reads correctly.
    // A saved 'dark' must be applied explicitly to override a light-mode OS.
    const saved = getSaved();
    if (saved === 'light') {
      applyTheme(true);
    } else if (saved === 'dark') {
      applyTheme(false);
    } else if (mq && mq.matches) {
      applyTheme(true);
    }
    // else: unsaved + OS dark — leave the base default; button stays "[ light ]".

    // Manual toggle: user explicitly chooses — flicker + click sound + persist
    toggle.addEventListener('click', function () {
      const isLight = root.getAttribute('data-theme') === 'light';
      triggerHaptic();
      playClickSound();
      flickerTransition();
      applyTheme(!isLight);
      setSaved(isLight ? 'dark' : 'light');
    });

    // System theme change: follow OS only if user hasn't manually chosen.
    // Silent (no click sound) — automatic change, not user-initiated.
    if (mq) {
      const handler = function (e) {
        if (getSaved() === null) {
          flickerTransition();
          applyTheme(e.matches);
        }
      };
      if (mq.addEventListener) {
        mq.addEventListener('change', handler);
      }
    }
  }

  // -----------------------------------------------------------
  // Section fade-in via IntersectionObserver (index.html)
  // -----------------------------------------------------------
  const sections = document.querySelectorAll('section');
  if (sections.length && 'IntersectionObserver' in window) {
    const observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) entry.target.classList.add('visible');
      });
    }, { threshold: 0.1, rootMargin: '0px 0px -40px 0px' });

    sections.forEach(function (s) { observer.observe(s); });
  }

  // -----------------------------------------------------------
  // Print button (resume.html)
  // -----------------------------------------------------------
  const printBtn = document.getElementById('printButton');
  if (printBtn) {
    printBtn.addEventListener('click', function () {
      playClickSound();
      window.print();
    });
  }

  // -----------------------------------------------------------
  // Save-to-contacts cue (index.html). A vCard download is a control
  // action, not navigation, so it carries the same click + haptic as the
  // toggle. The download doesn't unload the page, so the sound isn't cut off.
  // -----------------------------------------------------------
  const saveContactsBtn = document.getElementById('saveContactsBtn');
  if (saveContactsBtn) {
    saveContactsBtn.addEventListener('click', function () {
      playClickSound();
      triggerHaptic();
    });
  }

  // -----------------------------------------------------------
  // 404 path populator (404.html) — fills in the path the
  // visitor tried to load. No-ops on pages without the markers.
  // -----------------------------------------------------------
  const pathTargets = document.querySelectorAll('#requestedPath, #requestedPath2');
  if (pathTargets.length) {
    let path = window.location.pathname;
    if (path === '/' || path === '/404.html' || path === '/404') {
      path = '/unknown-resource';
    }
    pathTargets.forEach(function (el) { el.textContent = path; });
  }

  // -----------------------------------------------------------
  // Keyboard shortcuts — invisible by design, for the keyboard and
  // the curious. 't' toggles the theme (reusing the toggle's own
  // flicker + click + persistence); 'g' then h/r/c/o/a/s navigates (home,
  // resume, colophon, opinions, archive, subscribe), vim/gmail style; '?' prints the list
  // to the console, matching the greeting above. Ignored while a
  // field is focused or a browser modifier is held, so native
  // shortcuts (Cmd-T and friends) are never hijacked.
  // -----------------------------------------------------------
  (function keyboardShortcuts() {
    let goArmed = false, goTimer = null;
    function disarm() { goArmed = false; if (goTimer) { clearTimeout(goTimer); goTimer = null; } }

    document.addEventListener('keydown', function (e) {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      var el = e.target;
      if (el && (el.isContentEditable || /^(input|textarea|select)$/i.test(el.tagName))) return;

      var k = e.key;

      if (goArmed) {
        var dest = { h: '/', r: '/resume', c: '/colophon', o: '/opinions', a: '/archive', s: '/subscribe' }[String(k).toLowerCase()];
        disarm();
        if (dest) { e.preventDefault(); window.location.href = dest; }
        return;
      }

      if (k === 't' || k === 'T') {
        var tg = document.getElementById('themeToggle');
        if (tg) { e.preventDefault(); tg.click(); }
      } else if (k === 'g' || k === 'G') {
        goArmed = true;
        goTimer = setTimeout(disarm, 1200);
      } else if (k === '?') {
        try {
          console.log('%ckeyboard', 'color:#ff9e5e;font-family:ui-monospace,monospace;font-weight:600;');
          console.log(
            '%ct        toggle theme\ng h      home\ng r      resume\ng c      colophon\ng o      opinions\ng a      archive\ng s      subscribe\n?        this list',
            'color:#807a72;font-family:ui-monospace,monospace;line-height:1.6;'
          );
        } catch (e2) { /* noop */ }
      }
    });
  })();
})();

// The installable layer: register the service worker that makes the site a
// home-screen app with offline reading. Registration is the only line the
// pages need; the strategy lives in /sw.js.
if ("serviceWorker" in navigator) {
  window.addEventListener("load", function () {
    navigator.serviceWorker.register("/sw.js");
  });
}

// The install affordance on the Watch: one link that triggers the native
// install prompt where the platform offers one (Chromium), reveals the
// two-tap instructions where it does not (iPhone), and stays hidden once
// the app is already installed.
(function () {
  var row = document.getElementById("installRow");
  if (!row) return;
  var standalone = window.matchMedia("(display-mode: standalone)").matches ||
                   window.navigator.standalone === true;
  if (standalone) return;
  var go = document.getElementById("installGo");
  var how = document.getElementById("installHow");
  var deferred = null;
  window.addEventListener("beforeinstallprompt", function (e) {
    e.preventDefault();
    deferred = e;
    row.hidden = false;
  });
  var ios = /iPad|iPhone|iPod/.test(navigator.userAgent) ||
            (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  if (ios) row.hidden = false;
  go.addEventListener("click", function (e) {
    e.preventDefault();
    if (deferred) { deferred.prompt(); deferred = null; row.hidden = true; return; }
    how.hidden = !how.hidden;
  });
})();
