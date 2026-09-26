// Physical AI Radar: the Pages site's one script. Hand-written, no build step, shared
// by the landing pages (pairadar/site.py) and the Jekyll layout. Every page reads
// without it; it adds sharing, the lane filter, the day's poster and some motion.
(() => {
  "use strict";
  const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const data = (() => {
    const node = document.getElementById("share-data");
    try { return node ? JSON.parse(node.textContent) : null; } catch (error) { return null; }
  })();

  // Copy with a short confirmation on the button; a prompt where the clipboard is shut.
  async function copy(button, text, done) {
    const label = button.textContent;
    try { await navigator.clipboard.writeText(text); }
    catch (error) { window.prompt(label, text); return; }
    button.textContent = done;
    setTimeout(() => { button.textContent = label; }, 2000);
  }

  // Share: the system sheet where there is one, the clipboard where there is not.
  for (const button of document.querySelectorAll("button.share")) {
    button.addEventListener("click", async () => {
      const {url, text, done} = button.dataset;
      if (navigator.share) {
        try { await navigator.share({title: text, text, url}); return; }
        catch (error) { if (error.name === "AbortError") return; }
      }
      copy(button, `${text} ${url}`, done);
    });
  }

  const digest = document.querySelector("button.digest");
  if (digest && data) digest.addEventListener("click", () => copy(digest, data.digest, digest.dataset.done));

  // The lane filter: one lane at a time, or all of them.
  const chips = [...document.querySelectorAll("button.chip-filter")];
  for (const chip of chips) {
    chip.addEventListener("click", () => {
      const lane = chip.dataset.lane;
      for (const other of chips) other.setAttribute("aria-pressed", String(other === chip));
      for (const card of document.querySelectorAll(".grid .card")) {
        card.hidden = Boolean(lane) && card.dataset.lane !== lane;
      }
    });
  }

  // Motion, only for those who have not asked for less of it.
  if (!calm) {
    for (const dd of document.querySelectorAll(".stats dd")) {
      const target = Number(dd.textContent);
      if (!Number.isInteger(target) || target < 2) continue;
      const start = performance.now();
      let done = false;
      const tick = (now) => {
        if (done) return;
        const t = Math.min(1, Math.max(0, now - start) / 900);
        dd.textContent = String(Math.round(target * (1 - (1 - t) ** 3)));
        if (t < 1) requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
      // a hidden tab stops animation frames; the number must still end right
      setTimeout(() => { done = true; dd.textContent = String(target); }, 1200);
    }
    if ("IntersectionObserver" in window) {
      document.documentElement.classList.add("reveal");
      const seen = new IntersectionObserver((entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) { entry.target.classList.add("in"); seen.unobserve(entry.target); }
        }
      }, {rootMargin: "0px 0px -8% 0px"});
      document.querySelectorAll(".grid .card, .trend > div").forEach((node, index) => {
        node.style.setProperty("--i", String(index % 3));
        seen.observe(node);
      });
    }
  }

  // The poster: 1080x1440, the 3:4 that Xiaohongshu and WeChat Moments show whole.
  const posterButton = document.querySelector("button.poster");
  const dialog = document.querySelector("dialog.poster-dialog");
  if (!posterButton || !dialog || !data || !window.HTMLCanvasElement) return;

  const W = 1080, H = 1440, PAD = 72;
  const sans = getComputedStyle(document.body).fontFamily;
  const mono = 'ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace';
  const WIDE = /[⺀-鿿぀-ヿ가-힯豈-﫿＀-￯]/;

  // Greedy wrapping that breaks Latin at spaces and CJK anywhere, with an ellipsis
  // when the text runs past `max` lines.
  function wrap(ctx, text, width, max) {
    const tokens = text.match(/[⺀-鿿぀-ヿ가-힯豈-﫿＀-￯]|[^\s⺀-鿿぀-ヿ가-힯豈-﫿＀-￯]+\s*|\s+/g) || [];
    const lines = [];
    let line = "";
    for (let token of tokens) {
      while (ctx.measureText(token.trimEnd()).width > width && !WIDE.test(token)) {
        // a word wider than the line is cut where it stops fitting
        let cut = token.length - 1;
        while (cut > 1 && ctx.measureText(line + token.slice(0, cut)).width > width) cut--;
        lines.push(line + token.slice(0, cut));
        line = "";
        token = token.slice(cut);
      }
      if (ctx.measureText((line + token).trimEnd()).width > width && line) {
        lines.push(line.trimEnd());
        line = token.trimStart();
      } else {
        line += token;
      }
    }
    if (line.trim()) lines.push(line.trimEnd());
    if (lines.length <= max) return lines;
    let last = lines[max - 1];
    while (last && ctx.measureText(last + "…").width > width) last = last.slice(0, -1);
    return [...lines.slice(0, max - 1), last.trimEnd() + "…"];
  }

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function draw() {
    const canvas = document.createElement("canvas");
    canvas.width = W;
    canvas.height = H;
    const ctx = canvas.getContext("2d");

    // plate: the site's night sky, its two glows and its grid
    ctx.fillStyle = "#05070d";
    ctx.fillRect(0, 0, W, H);
    for (const [x, y, r, color] of [[W * 0.9, -80, 900, "rgba(139,92,246,.30)"], [-120, 260, 700, "rgba(52,211,153,.16)"]]) {
      const glow = ctx.createRadialGradient(x, y, 0, x, y, r);
      glow.addColorStop(0, color);
      glow.addColorStop(1, "rgba(5,7,13,0)");
      ctx.fillStyle = glow;
      ctx.fillRect(0, 0, W, H);
    }
    ctx.strokeStyle = "rgba(148,163,184,.06)";
    ctx.lineWidth = 1;
    for (let x = 0; x <= W; x += 54) { ctx.beginPath(); ctx.moveTo(x + 0.5, 0); ctx.lineTo(x + 0.5, H); ctx.stroke(); }
    for (let y = 0; y <= H; y += 54) { ctx.beginPath(); ctx.moveTo(0, y + 0.5); ctx.lineTo(W, y + 0.5); ctx.stroke(); }

    // masthead
    let y = PAD + 8;
    ctx.textBaseline = "middle";
    ctx.fillStyle = "#34d399";
    ctx.beginPath(); ctx.arc(PAD + 8, y, 8, 0, Math.PI * 2); ctx.fill();
    ctx.font = `700 30px ${sans}`;
    ctx.fillStyle = "#e6edf3";
    ctx.fillText(data.title, PAD + 30, y);
    ctx.font = `600 28px ${mono}`;
    ctx.fillStyle = "#34d399";
    ctx.textAlign = "right";
    ctx.fillText(data.date, W - PAD, y);
    ctx.textAlign = "left";

    // headline in the site's gradient
    y += 58;
    ctx.textBaseline = "top";
    ctx.font = `800 60px ${sans}`;
    const shine = ctx.createLinearGradient(PAD, 0, W - PAD, 0);
    shine.addColorStop(0.1, "#ffffff");
    shine.addColorStop(0.55, "#c4b5fd");
    shine.addColorStop(1, "#6ee7b7");
    ctx.fillStyle = shine;
    for (const line of wrap(ctx, data.headline, W - 2 * PAD, 2)) { ctx.fillText(line, PAD, y); y += 74; }
    y += 18;

    // the picks, one row each: rank, lane, evidence, title, lead figure
    const picks = data.picks.slice(0, 8);
    const footTop = H - PAD - 180;
    const rowH = Math.min(132, (footTop - 24 - y) / Math.max(picks.length, 1));
    const titleSize = 24, twoLines = rowH >= 104;
    for (const pick of picks) {
      roundRect(ctx, PAD, y, W - 2 * PAD, rowH - 12, 16);
      ctx.fillStyle = "rgba(16,24,43,.82)";
      ctx.fill();
      ctx.strokeStyle = "rgba(148,163,184,.16)";
      ctx.stroke();
      ctx.fillStyle = pick.color;
      roundRect(ctx, PAD, y, 6, rowH - 12, 3);
      ctx.fill();

      const left = PAD + 28;
      ctx.textBaseline = "top";
      ctx.font = `700 22px ${mono}`;
      ctx.fillStyle = "#8b98ad";
      ctx.fillText(String(pick.rank).padStart(2, "0"), left, y + 11);
      ctx.font = `700 20px ${sans}`;
      ctx.fillStyle = pick.color;
      ctx.fillText(pick.lane, left + 44, y + 12);
      const badge = `${pick.evidence} · ${data.evidence[pick.evidence] || ""}`;
      ctx.font = `700 18px ${mono}`;
      const bw = ctx.measureText(badge).width + 20;
      const color = {O: "#34d399", R: "#60a5fa", M: "#fbbf24"}[pick.evidence] || "#8b98ad";
      roundRect(ctx, W - PAD - 20 - bw, y + 8, bw, 28, 7);
      ctx.strokeStyle = color;
      ctx.stroke();
      ctx.fillStyle = color;
      ctx.fillText(badge, W - PAD - 10 - bw, y + 13);

      let right = W - PAD - 28;
      if (pick.figure) {
        ctx.font = `800 34px ${mono}`;
        ctx.fillStyle = pick.color;
        ctx.textAlign = "right";
        ctx.fillText(pick.figure, right, y + (twoLines ? 56 : 48));
        ctx.textAlign = "left";
        right -= ctx.measureText(pick.figure).width + 24;
      }
      ctx.font = `600 ${titleSize}px ${sans}`;
      ctx.fillStyle = "#e6edf3";
      const lines = wrap(ctx, pick.title, right - left, twoLines ? 2 : 1);
      lines.forEach((line, index) => ctx.fillText(line, left, y + 41 + index * (titleSize + 5)));
      y += rowH;
    }

    // foot: the way back, as a QR code and as text
    // whole pixels per module and the four-module quiet zone scanners expect
    const cell = Math.max(3, Math.floor(156 / data.qr.length));
    const size = cell * data.qr.length, quiet = 4 * cell;
    const qx = W - PAD - quiet - size, qy = H - PAD - quiet - size;
    ctx.fillStyle = "#ffffff";
    roundRect(ctx, qx - quiet, qy - quiet, size + 2 * quiet, size + 2 * quiet, 12);
    ctx.fill();
    ctx.fillStyle = "#05070d";
    data.qr.forEach((row, r) => {
      for (let c = 0; c < row.length; c++) {
        if (row[c] === "1") ctx.fillRect(qx + c * cell, qy + r * cell, cell, cell);
      }
    });
    ctx.textBaseline = "alphabetic";
    ctx.font = `700 30px ${sans}`;
    ctx.fillStyle = "#e6edf3";
    ctx.fillText(data.scan, PAD, H - PAD - 88);
    ctx.font = `600 24px ${mono}`;
    ctx.fillStyle = "#34d399";
    ctx.fillText(data.site, PAD, H - PAD - 44);
    ctx.font = `500 20px ${sans}`;
    ctx.fillStyle = "#8b98ad";
    ctx.fillText(Object.entries(data.evidence).map(([k, v]) => `${k} ${v}`).join("  ·  "), PAD, H - PAD - 8);
    return canvas;
  }

  const image = dialog.querySelector("img");
  const download = dialog.querySelector("a.download");
  const shareImage = dialog.querySelector("button.share-image");
  let file = null;
  posterButton.addEventListener("click", async () => {
    if (document.fonts && document.fonts.ready) await document.fonts.ready;
    const canvas = draw();
    const url = canvas.toDataURL("image/png");
    image.src = url;
    download.href = url;
    file = null;
    canvas.toBlob((blob) => {
      if (!blob) return;
      file = new File([blob], download.getAttribute("download"), {type: "image/png"});
      shareImage.hidden = !(navigator.canShare && navigator.canShare({files: [file]}));
    }, "image/png");
    dialog.showModal();
  });
  shareImage.addEventListener("click", async () => {
    if (!file) return;
    try { await navigator.share({files: [file], title: `${data.title} · ${data.date}`, text: data.url}); }
    catch (error) { /* dismissed */ }
  });
  dialog.querySelector("button.close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); });
})();
