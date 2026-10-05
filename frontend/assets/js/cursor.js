/* 3D Bubble Cursor & Interactive Depth Optics for Natasha.
   Provides a physical, tactile glass orb cursor with spring inertia, specular caustics,
   magnetic snapping, text selection safety, and an accessible fallback/disable toggle. */

const STORAGE_KEY = "natasha.bubble_cursor";

class BubbleCursorSystem {
  constructor() {
    this.enabled = localStorage.getItem(STORAGE_KEY) !== "false";
    // Check if user prefers reduced motion or has touch screen
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      this.enabled = false;
    }
    if ("ontouchstart" in window || navigator.maxTouchPoints > 0) {
      this.enabled = false;
    }

    this.mouseX = window.innerWidth / 2;
    this.mouseY = window.innerHeight / 2;
    this.currentX = this.mouseX;
    this.currentY = this.mouseY;
    this.vx = 0;
    this.vy = 0;
    this.isHovering = false;
    this.isPressed = false;
    this.isDragging = false;
    this.isLoading = false;
    this.magneticTarget = null;
    this.initialized = false;
  }

  init() {
    if (this.initialized) return;
    this.initialized = true;

    // Create DOM structure if not present
    let container = document.getElementById("bubble-container");
    if (!container) {
      container = document.createElement("div");
      container.id = "bubble-container";
      container.setAttribute("aria-hidden", "true");
      container.innerHTML = `
        <div id="bubble-cursor-wrapper">
          <div id="bubble-cursor"></div>
          <div id="bubble-shadow"></div>
        </div>
      `;
      document.body.appendChild(container);
    }

    this.dom = {
      container,
      wrapper: document.getElementById("bubble-cursor-wrapper"),
      cursor: document.getElementById("bubble-cursor"),
      shadow: document.getElementById("bubble-shadow"),
    };

    if (!this.enabled) {
      this.dom.container.style.display = "none";
    }

    this.bindEvents();
    this.startPhysicsLoop();
    this.init3DTilt();
  }

  bindEvents() {
    window.addEventListener("mousemove", (e) => {
      this.mouseX = e.clientX;
      this.mouseY = e.clientY;
    }, { passive: true });

    window.addEventListener("mousedown", (e) => {
      if (!this.enabled) return;
      this.isPressed = true;
      if (this.dom.cursor) {
        this.dom.cursor.classList.add("cursor-pressed");
      }

      // Cavitation ripple effect
      const ripple = document.createElement("div");
      ripple.className = "bubble-ripple";
      ripple.style.left = `${e.clientX}px`;
      ripple.style.top = `${e.clientY}px`;
      this.dom.container.appendChild(ripple);
      setTimeout(() => ripple.remove(), 600);
    });

    window.addEventListener("mouseup", () => {
      this.isPressed = false;
      this.isDragging = false;
      if (this.dom.cursor) {
        this.dom.cursor.classList.remove("cursor-pressed");
        this.dom.cursor.classList.remove("cursor-dragging");
      }
    });

    // Delegate hover states over interactive nodes
    document.addEventListener("mouseover", (e) => {
      if (!this.enabled || !this.dom.cursor) return;
      const target = e.target.closest("a, button, input, textarea, select, [role='button'], [role='switch'], .tactile-card, .btn, .chip, .nav__item, .filter-chip");
      if (target) {
        this.isHovering = true;
        const tag = target.tagName.toLowerCase();
        if (tag === "input" || tag === "textarea") {
          this.dom.cursor.classList.add("cursor-text");
          this.dom.cursor.classList.remove("cursor-hover");
        } else {
          this.dom.cursor.classList.add("cursor-hover");
          this.dom.cursor.classList.remove("cursor-text");
          if (target.classList.contains("btn") || target.classList.contains("tactile-button")) {
            this.magneticTarget = target;
          }
        }
      }
    });

    document.addEventListener("mouseout", (e) => {
      if (!this.enabled || !this.dom.cursor) return;
      const target = e.target.closest("a, button, input, textarea, select, [role='button'], [role='switch'], .tactile-card, .btn, .chip, .nav__item, .filter-chip");
      if (target) {
        this.isHovering = false;
        this.magneticTarget = null;
        this.dom.cursor.classList.remove("cursor-hover");
        this.dom.cursor.classList.remove("cursor-text");
      }
    });
  }

  startPhysicsLoop() {
    const update = () => {
      if (this.enabled && this.dom.wrapper) {
        let targetX = this.mouseX;
        let targetY = this.mouseY;

        // Subtle magnetic attraction to primary buttons
        if (this.magneticTarget && !this.isPressed) {
          try {
            const rect = this.magneticTarget.getBoundingClientRect();
            const btnCenterX = rect.left + rect.width / 2;
            const btnCenterY = rect.top + rect.height / 2;
            targetX = targetX + (btnCenterX - targetX) * 0.22;
            targetY = targetY + (btnCenterY - targetY) * 0.22;
          } catch {
            this.magneticTarget = null;
          }
        }

        // Spring ease
        const spring = 0.28;
        const friction = 0.68;
        const ax = (targetX - this.currentX) * spring;
        const ay = (targetY - this.currentY) * spring;

        this.vx = (this.vx + ax) * friction;
        this.vy = (this.vy + ay) * friction;

        this.currentX += this.vx;
        this.currentY += this.vy;

        // Calculate stretch and angle based on speed
        const speed = Math.hypot(this.vx, this.vy);
        const stretch = Math.min(speed * 0.015, 0.25);
        const angle = Math.atan2(this.vy, this.vx) * (180 / Math.PI);

        if (this.dom.wrapper) {
          this.dom.wrapper.style.transform = `translate3d(${this.currentX}px, ${this.currentY}px, 0)`;
        }

        if (this.dom.cursor && !this.isPressed && !this.dom.cursor.classList.contains("cursor-text")) {
          this.dom.cursor.style.transform = `rotate(${angle}deg) scale(${1 + stretch}, ${1 - stretch * 0.5})`;
        } else if (this.dom.cursor && this.dom.cursor.classList.contains("cursor-text")) {
          this.dom.cursor.style.transform = "none";
        }

        if (this.dom.shadow) {
          const shadowX = -this.vx * 0.4;
          const shadowY = 16 - this.vy * 0.3;
          this.dom.shadow.style.transform = `translate(calc(-50% + ${shadowX}px), ${shadowY}px) scale(${1 - stretch * 0.4})`;
        }
      }

      requestAnimationFrame(update);
    };

    requestAnimationFrame(update);
  }

  init3DTilt() {
    document.addEventListener("mousemove", (e) => {
      const card = e.target.closest("[data-3d-tilt='true'], .tactile-card");
      if (!card) return;
      const rect = card.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      const centerX = rect.width / 2;
      const centerY = rect.height / 2;

      const rotateX = ((y - centerY) / centerY) * -4.5;
      const rotateY = ((x - centerX) / centerX) * 4.5;

      card.style.transform = `perspective(1000px) rotateX(${rotateX.toFixed(2)}deg) rotateY(${rotateY.toFixed(2)}deg) scale3d(1.008, 1.008, 1.008)`;
      card.style.setProperty("--mouse-x", `${(x / rect.width) * 100}%`);
      card.style.setProperty("--mouse-y", `${(y / rect.height) * 100}%`);
    });

    document.addEventListener("mouseout", (e) => {
      const card = e.target.closest("[data-3d-tilt='true'], .tactile-card");
      if (card && (!e.relatedTarget || !card.contains(e.relatedTarget))) {
        card.style.transform = "perspective(1000px) rotateX(0deg) rotateY(0deg) scale3d(1, 1, 1)";
      }
    });
  }

  setLoading(loading) {
    this.isLoading = Boolean(loading);
    if (this.dom && this.dom.cursor) {
      this.dom.cursor.classList.toggle("cursor-loading", this.isLoading);
    }
  }

  enable() {
    return this.toggle(true);
  }

  disable() {
    return this.toggle(false);
  }

  isEnabled() {
    return this.enabled;
  }

  toggle(enabled) {
    this.enabled = typeof enabled === "boolean" ? enabled : !this.enabled;
    localStorage.setItem(STORAGE_KEY, String(this.enabled));
    if (this.dom && this.dom.container) {
      this.dom.container.style.display = this.enabled ? "block" : "none";
    }
    return this.enabled;
  }
}

export const bubbleCursor = new BubbleCursorSystem();
