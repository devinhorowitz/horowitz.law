// =============================================================
// horowitz.law — app.js
// Single shared script for both index.html and resume.html.
// Feature-detects: only the bits relevant to the current page run.
// =============================================================

(function () {
  'use strict';

  // -----------------------------------------------------------
  // Theme toggle (both pages)
  // -----------------------------------------------------------
  const toggle = document.getElementById('themeToggle');
  const root = document.documentElement;

  function getSaved() {
    try { return localStorage.getItem('theme'); } catch (e) { return null; }
  }
  function setSaved(v) {
    try { localStorage.setItem('theme', v); } catch (e) { /* noop */ }
  }

  if (toggle) {
    // First-visit precedence: explicit user choice > OS preference > dark default
    const saved = getSaved();
    const prefersLight =
      saved === null &&
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-color-scheme: light)').matches;

    if (saved === 'light' || prefersLight) {
      root.setAttribute('data-theme', 'light');
      toggle.textContent = '[ dark ]';
      toggle.setAttribute('aria-pressed', 'true');
    }

    toggle.addEventListener('click', function () {
      const isLight = root.getAttribute('data-theme') === 'light';
      if (isLight) {
        root.removeAttribute('data-theme');
        toggle.textContent = '[ light ]';
        toggle.setAttribute('aria-pressed', 'false');
        setSaved('dark');
      } else {
        root.setAttribute('data-theme', 'light');
        toggle.textContent = '[ dark ]';
        toggle.setAttribute('aria-pressed', 'true');
        setSaved('light');
      }
    });
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
})();
