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

  // One-time migration: copy legacy 'theme' key to namespaced 'horowitz-theme'.
  // Can be removed after a reasonable transition window (3-6 months).
  try {
    const legacy = localStorage.getItem('theme');
    if (legacy && !localStorage.getItem('horowitz-theme')) {
      localStorage.setItem('horowitz-theme', legacy);
      localStorage.removeItem('theme');
    }
  } catch (e) { /* noop */ }

  if (toggle) {
    const mq = window.matchMedia ? window.matchMedia('(prefers-color-scheme: light)') : null;
    const reduceMotion = window.matchMedia &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches;

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

    // Brief haptic pulse on supported devices (Android browsers). The
    // double-pulse pattern mimics the bistable click of a real toggle switch.
    // Silently no-ops on iOS (Apple doesn't implement Web Vibration) and on
    // desktop (no vibration hardware). Respects prefers-reduced-motion.
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

    // Synthesized "tactile click" via Web Audio API. No asset file needed.
    // Slight per-click randomization in frequency and volume matches the
    // analog feel of the typing and flicker animations. Skipped under
    // prefers-reduced-motion. AudioContext is created lazily on first use
    // because browser autoplay policies require user gesture initialization.
    let audioCtx = null;
    function playClickSound() {
      if (reduceMotion) return;
      try {
        if (!audioCtx) {
          const Ctx = window.AudioContext || window.webkitAudioContext;
          if (!Ctx) return;
          audioCtx = new Ctx();
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
      } else if (mq.addListener) {
        mq.addListener(handler); // Safari < 14 fallback
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
      window.print();
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
})();
