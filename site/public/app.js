(() => {
  'use strict';

  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const header = document.querySelector('[data-header]');
  const setHeader = () => header?.classList.toggle('is-scrolled', window.scrollY > 18);
  setHeader();
  window.addEventListener('scroll', setHeader, { passive: true });

  document.querySelectorAll('[data-year]').forEach((node) => {
    node.textContent = String(new Date().getFullYear());
  });

  document.querySelectorAll('[data-max-cta]').forEach((link) => {
    link.addEventListener('click', () => {
      window.dispatchEvent(new CustomEvent('pixora:cta', {
        detail: { placement: link.dataset.maxCta }
      }));
    });
  });

  document.querySelectorAll('details').forEach((item) => {
    item.addEventListener('toggle', () => {
      const marker = item.querySelector('summary span');
      if (marker) marker.textContent = item.open ? '−' : '+';
    });
  });

  const revealItems = document.querySelectorAll('.reveal');
  if (reducedMotion || !('IntersectionObserver' in window)) {
    revealItems.forEach((item) => item.classList.add('is-visible'));
  } else {
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add('is-visible');
          observer.unobserve(entry.target);
        }
      });
    }, { rootMargin: '0px 0px -8% 0px', threshold: 0.08 });
    revealItems.forEach((item) => observer.observe(item));
  }
})();
