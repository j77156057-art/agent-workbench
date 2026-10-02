/* DocMind · 极简亮色落地草案 · 交互层（入场序列 + 指标 count-up + 降级） */
(function () {
  "use strict";
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const reveals = document.querySelectorAll(".reveal");
  if (reduceMotion || !("IntersectionObserver" in window)) {
    reveals.forEach((el) => el.classList.add("in"));
  } else {
    const io = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) { entry.target.classList.add("in"); io.unobserve(entry.target); }
      });
    }, { threshold: 0.16, rootMargin: "0px 0px -8% 0px" });
    reveals.forEach((el) => io.observe(el));
  }

  function unit(suffix) { return suffix ? '<span class="u">' + suffix + "</span>" : ""; }
  function format(v) { return Math.round(v).toLocaleString("en-US"); }
  function countUp(el) {
    const target = parseFloat(el.dataset.count || "0");
    const suffix = el.dataset.suffix || "";
    if (reduceMotion) { el.innerHTML = format(target) + unit(suffix); return; }
    const dur = 1100, start = performance.now();
    function tick(now) {
      const p = Math.min(1, (now - start) / dur);
      const eased = 1 - Math.pow(1 - p, 3);
      el.innerHTML = format(target * eased) + unit(suffix);
      if (p < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  }
  const counters = document.querySelectorAll(".metric .num[data-count]");
  if (reduceMotion || !("IntersectionObserver" in window)) {
    counters.forEach(countUp);
  } else {
    const co = new IntersectionObserver((entries) => {
      entries.forEach((entry) => { if (entry.isIntersecting) { countUp(entry.target); co.unobserve(entry.target); } });
    }, { threshold: 0.5 });
    counters.forEach((el) => co.observe(el));
  }
})();
