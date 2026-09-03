document.addEventListener("DOMContentLoaded", () => {
  lucide.createIcons();
  initMockGrid();
  initBgCanvas();
  initCursorGlow();
  initHeroTilt();
  initReveal();
  initMobileMenu();
});

// =====================================================================
// Mobile menu
// =====================================================================
function initMobileMenu() {
  const menuBtn = document.getElementById("mobile-menu-btn");
  const mobileMenu = document.getElementById("mobile-menu");

  if (menuBtn && mobileMenu) {
    menuBtn.addEventListener("click", () => {
      mobileMenu.classList.toggle("hidden");
    });
    mobileMenu.querySelectorAll("a").forEach((link) => {
      link.addEventListener("click", () => {
        mobileMenu.classList.add("hidden");
      });
    });
  }

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
}

// =====================================================================
// Cursor glow
// =====================================================================
function initCursorGlow() {
  let ticking = false;
  const update = (x, y) => {
    document.body.style.setProperty('--cursor-x', x + 'px');
    document.body.style.setProperty('--cursor-y', y + 'px');
  };

  window.addEventListener('mousemove', (e) => {
    if (!ticking) {
      window.requestAnimationFrame(() => {
        update(e.clientX, e.clientY);
        ticking = false;
      });
      ticking = true;
    }
  }, { passive: true });

  if (window.matchMedia('(pointer: coarse)').matches) {
    document.getElementById('cursor-glow').style.display = 'none';
  }
}

// =====================================================================
// Hero 3D tilt + parallax
// =====================================================================
function initHeroTilt() {
  const grid = document.getElementById('mock-grid');
  const hero = document.querySelector('.hero-tilt');
  if (!grid) return;

  const wrapper = grid.closest('.hero-tilt') || grid.parentElement;
  let raf = null;
  let targetX = 0, targetY = 0;
  let currentX = 0, currentY = 0;

  const update = () => {
    currentX += (targetX - currentX) * 0.08;
    currentY += (targetY - currentY) * 0.08;
    grid.style.transform = `rotateY(${currentX * 8}deg) rotateX(${-currentY * 8}deg) translateZ(20px)`;
    raf = null;
  };

  document.addEventListener('mousemove', (e) => {
    const rect = wrapper.getBoundingClientRect();
    const x = (e.clientX - rect.left) / rect.width - 0.5;
    const y = (e.clientY - rect.top) / rect.height - 0.5;
    targetX = Math.max(-1, Math.min(1, x));
    targetY = Math.max(-1, Math.min(1, y));
    if (raf === null) raf = requestAnimationFrame(update);
  }, { passive: true });
}

// =====================================================================
// Scroll reveal with stagger
// =====================================================================
function initReveal() {
  const elements = document.querySelectorAll(
    "h2, h3, .feature-card, .step-card, .download-card, .donate-btn"
  );

  elements.forEach((el) => {
    el.classList.add("reveal");
  });

  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          const parent = entry.target.parentElement;
          const siblings = parent ? Array.from(parent.querySelectorAll('.reveal')) : [];
          const index = siblings.indexOf(entry.target);
          const delay = Math.max(0, (index >= 0 ? index : 0) * 80);
          setTimeout(() => {
            entry.target.classList.add("visible");
          }, delay);
          observer.unobserve(entry.target);
        }
      });
    },
    { threshold: 0.12, rootMargin: "0px 0px -40px 0px" }
  );

  elements.forEach((el) => observer.observe(el));
}

// =====================================================================
// Mock device grid
// =====================================================================
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

// =====================================================================
// Interactive background canvas (particles + mouse)
// =====================================================================
function initBgCanvas() {
  const canvas = document.getElementById("bg-canvas");
  if (!canvas) return;

  const ctx = canvas.getContext("2d");
  let width = 0;
  let height = 0;
  let particles = [];
  const colors = [
    "rgba(212, 175, 53, 0.55)",
    "rgba(31, 143, 255, 0.45)",
    "rgba(255, 92, 92, 0.45)",
    "rgba(255, 255, 255, 0.35)",
  ];
  let mouse = { x: -9999, y: -9999 };

  function resize() {
    width = window.innerWidth;
    height = window.innerHeight;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
    canvas.style.width = width + "px";
    canvas.style.height = height + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const area = (width * height) / 1000000;
    const count = Math.min(80, Math.max(30, Math.floor(area * 22)));
    particles = [];
    for (let i = 0; i < count; i++) {
      particles.push({
        x: Math.random() * width,
        y: Math.random() * height,
        vx: (Math.random() - 0.5) * 0.45,
        vy: (Math.random() - 0.5) * 0.45,
        size: Math.random() * 1.8 + 0.6,
        color: colors[Math.floor(Math.random() * colors.length)],
        pulse: Math.random() * Math.PI * 2,
      });
    }
  }

  resize();

  window.addEventListener("resize", resize);

  document.addEventListener('mousemove', (e) => {
    mouse.x = e.clientX;
    mouse.y = e.clientY;
  }, { passive: true });

  document.addEventListener('mouseleave', () => {
    mouse.x = -9999;
    mouse.y = -9999;
  });

  function draw() {
    ctx.clearRect(0, 0, width, height);

    for (const p of particles) {
      p.x += p.vx;
      p.y += p.vy;
      p.pulse += 0.03;

      if (p.x < 0 || p.x > width) p.vx *= -1;
      if (p.y < 0 || p.y > height) p.vy *= -1;

      const dx = mouse.x - p.x;
      const dy = mouse.y - p.y;
      const dist = Math.hypot(dx, dy);
      if (dist < 180 && dist > 0) {
        const force = (180 - dist) / 180;
        p.x -= (dx / dist) * force * 0.8;
        p.y -= (dy / dist) * force * 0.8;
      }

      const pulseSize = p.size + Math.sin(p.pulse) * 0.3;
      ctx.beginPath();
      ctx.arc(p.x, p.y, Math.max(0.2, pulseSize), 0, Math.PI * 2);
      ctx.fillStyle = p.color;
      ctx.shadowColor = p.color;
      ctx.shadowBlur = 10;
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

        if (dist < 140) {
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.strokeStyle = `rgba(212, 175, 53, ${0.10 * (1 - dist / 140)})`;
          ctx.lineWidth = 0.5;
          ctx.stroke();
        }
      }

      if (mouse.x > -9000) {
        const a = particles[i];
        const dx = a.x - mouse.x;
        const dy = a.y - mouse.y;
        const dist = Math.hypot(dx, dy);
        if (dist < 220) {
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(mouse.x, mouse.y);
          ctx.strokeStyle = `rgba(212, 175, 53, ${0.12 * (1 - dist / 220)})`;
          ctx.lineWidth = 0.6;
          ctx.stroke();
        }
      }
    }

    requestAnimationFrame(draw);
  }

  draw();
}
