document.addEventListener("DOMContentLoaded", () => {
  // Icone Lucide
  lucide.createIcons();
  initMockGrid();
  initBgCanvas();

  // Menu mobile
  const menuBtn = document.getElementById("mobile-menu-btn");
  const mobileMenu = document.getElementById("mobile-menu");

  if (menuBtn && mobileMenu) {
    menuBtn.addEventListener("click", () => {
      mobileMenu.classList.toggle("hidden");
    });
    // Chiudi menu al click su un link
    mobileMenu.querySelectorAll("a").forEach((link) => {
      link.addEventListener("click", () => {
        mobileMenu.classList.add("hidden");
      });
    });
  }

  // Header ombra al scroll
  const nav = document.querySelector("nav");
  if (nav) {
    const updateNav = () => {
      if (window.scrollY > 8) {
        nav.classList.add("shadow-lg");
      } else {
        nav.classList.remove("shadow-lg");
      }
    };
    window.addEventListener("scroll", updateNav, { passive: true });
    updateNav();
  }

  // Animazione reveal al scroll
  const reveal = () => {
    const elements = document.querySelectorAll(
      "h2, h3, .feature-card, .step-card, .download-card, .donate-btn"
    );
    elements.forEach((el) => el.classList.add("reveal"));

    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            entry.target.classList.add("visible");
            observer.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.12, rootMargin: "0px 0px -40px 0px" }
    );

    elements.forEach((el) => observer.observe(el));
  };

  reveal();
});

function initMockGrid() {
  const grid = document.getElementById("mock-grid");
  if (!grid) return;

  const appIcons = [
    { icon: "▶️", color: "app-rose" },
    { icon: "📧", color: "app-blue" },
    { icon: "💬", color: "app-green" },
    { icon: "🗺️", color: "app-amber" },
    { icon: "⚙️", color: "app-slate" },
    { icon: "📱", color: "app-emerald" },
    { icon: "🎵", color: "app-cyan" },
    { icon: "📸", color: "app-violet" },
  ];

  const overlays = [
    "Streaming...",
    "Controllo...",
    "Install...",
    "Sync OK",
    "Ricerca...",
    "Broadcast",
  ];

  // Genera 9 dispositivi con app casuali
  for (let i = 0; i < 9; i++) {
    const apps = [];
    const pool = [...appIcons].sort(() => Math.random() - 0.5);
    for (let j = 0; j < 4; j++) {
      const app = pool[j];
      apps.push(`<div class="mock-app ${app.color}">${app.icon}</div>`);
    }

    const overlay = overlays[i % overlays.length];
    const device = document.createElement("div");
    device.className = "mock-device";
    device.innerHTML = `
      <div class="mock-notch"></div>
      <div class="mock-screen">
        <div class="mock-status">
          <span>${9 + (i % 15)}:${(i * 7) % 60 < 10 ? "0" : ""}${(i * 7) % 60}</span>
          <span>⚡ ${50 + ((i * 17) % 50)}%</span>
        </div>
        <div class="mock-apps">${apps.join("")}</div>
      </div>
      <div class="mock-overlay">
        <div class="spinner"></div>
        <span class="label">${overlay}</span>
        <div class="status-line"></div>
      </div>
    `;
    grid.appendChild(device);
  }

  // Ciclo di animazione: ogni ~1.4s un telefono "apre" un'app
  setInterval(() => {
    const devices = grid.querySelectorAll(".mock-device");
    if (!devices.length) return;
    const device = devices[Math.floor(Math.random() * devices.length)];
    const apps = device.querySelectorAll(".mock-app");
    const app = apps[Math.floor(Math.random() * apps.length)];
    const overlay = device.querySelector(".mock-overlay");

    app.classList.add("open");
    setTimeout(() => {
      overlay.classList.add("active");
      device.classList.add("active");
      setTimeout(() => {
        overlay.classList.remove("active");
        device.classList.remove("active");
        app.classList.remove("open");
      }, 1300);
    }, 260);
  }, 1400);
}

function initBgCanvas() {
  const canvas = document.getElementById("bg-canvas");
  if (!canvas) return;

  const ctx = canvas.getContext("2d");
  let width = 0;
  let height = 0;
  let particles = [];
  const count = 40;
  const colors = [
    "rgba(212, 175, 53, 0.45)",
    "rgba(31, 143, 255, 0.35)",
    "rgba(255, 92, 92, 0.35)",
  ];

  function resize() {
    width = window.innerWidth;
    height = window.innerHeight;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
    canvas.style.width = width + "px";
    canvas.style.height = height + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  resize();

  for (let i = 0; i < count; i++) {
    particles.push({
      x: Math.random() * width,
      y: Math.random() * height,
      vx: (Math.random() - 0.5) * 0.35,
      vy: (Math.random() - 0.5) * 0.35,
      size: Math.random() * 1.6 + 0.7,
      color: colors[Math.floor(Math.random() * colors.length)],
    });
  }

  function draw() {
    ctx.clearRect(0, 0, width, height);

    for (const p of particles) {
      p.x += p.vx;
      p.y += p.vy;

      if (p.x < 0 || p.x > width) p.vx *= -1;
      if (p.y < 0 || p.y > height) p.vy *= -1;

      ctx.beginPath();
      ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2);
      ctx.fillStyle = p.color;
      ctx.shadowColor = p.color;
      ctx.shadowBlur = 8;
      ctx.fill();
      ctx.shadowBlur = 0;
    }

    for (let i = 0; i < particles.length; i++) {
      for (let j = i + 1; j < particles.length; j++) {
        const a = particles[i];
        const b = particles[j];
        const dx = a.x - b.x;
        const dy = a.y - b.y;
        const dist = Math.hypot(dx, dy);

        if (dist < 130) {
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.strokeStyle = `rgba(255, 255, 255, ${0.05 * (1 - dist / 130)})`;
          ctx.lineWidth = 0.4;
          ctx.stroke();
        }
      }
    }

    requestAnimationFrame(draw);
  }

  window.addEventListener("resize", resize);
  draw();
}
