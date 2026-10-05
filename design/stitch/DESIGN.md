---
name: Obsidian Intelligence
colors:
  surface: '#131315'
  surface-dim: '#131315'
  surface-bright: '#39393b'
  surface-container-lowest: '#0e0e10'
  surface-container-low: '#1c1b1d'
  surface-container: '#201f22'
  surface-container-high: '#2a2a2c'
  surface-container-highest: '#353437'
  on-surface: '#e5e1e4'
  on-surface-variant: '#c4c7c8'
  inverse-surface: '#e5e1e4'
  inverse-on-surface: '#313032'
  outline: '#8e9192'
  outline-variant: '#444748'
  surface-tint: '#c6c6c7'
  primary: '#ffffff'
  on-primary: '#2f3131'
  primary-container: '#e2e2e2'
  on-primary-container: '#636565'
  inverse-primary: '#5d5f5f'
  secondary: '#c8c5ca'
  on-secondary: '#303033'
  secondary-container: '#47464a'
  on-secondary-container: '#b6b4b8'
  tertiary: '#ffffff'
  on-tertiary: '#303033'
  tertiary-container: '#e4e1e5'
  on-tertiary-container: '#656467'
  error: '#ffb4ab'
  on-error: '#690005'
  error-container: '#93000a'
  on-error-container: '#ffdad6'
  primary-fixed: '#e2e2e2'
  primary-fixed-dim: '#c6c6c7'
  on-primary-fixed: '#1a1c1c'
  on-primary-fixed-variant: '#454747'
  secondary-fixed: '#e4e1e6'
  secondary-fixed-dim: '#c8c5ca'
  on-secondary-fixed: '#1b1b1e'
  on-secondary-fixed-variant: '#47464a'
  tertiary-fixed: '#e4e1e5'
  tertiary-fixed-dim: '#c8c6c9'
  on-tertiary-fixed: '#1b1b1e'
  on-tertiary-fixed-variant: '#47464a'
  background: '#131315'
  on-background: '#e5e1e4'
  surface-variant: '#353437'
typography:
  display-lg:
    fontFamily: Inter
    fontSize: 40px
    fontWeight: '700'
    lineHeight: 48px
    letterSpacing: -0.025em
  display-lg-mobile:
    fontFamily: Inter
    fontSize: 30px
    fontWeight: '700'
    lineHeight: 38px
    letterSpacing: -0.02em
  headline-lg:
    fontFamily: Inter
    fontSize: 28px
    fontWeight: '600'
    lineHeight: 36px
    letterSpacing: -0.02em
  headline-md:
    fontFamily: Inter
    fontSize: 22px
    fontWeight: '600'
    lineHeight: 28px
    letterSpacing: -0.015em
  body-large:
    fontFamily: Inter
    fontSize: 17px
    fontWeight: '400'
    lineHeight: 26px
    letterSpacing: -0.005em
  body-base:
    fontFamily: Inter
    fontSize: 15px
    fontWeight: '400'
    lineHeight: 24px
    letterSpacing: 0em
  body-bold:
    fontFamily: Inter
    fontSize: 15px
    fontWeight: '600'
    lineHeight: 24px
    letterSpacing: 0em
  body-muted:
    fontFamily: Inter
    fontSize: 14px
    fontWeight: '400'
    lineHeight: 20px
    letterSpacing: 0em
  code-base:
    fontFamily: JetBrains Mono
    fontSize: 13px
    fontWeight: '400'
    lineHeight: 20px
    letterSpacing: 0em
  label-caps:
    fontFamily: Inter
    fontSize: 11px
    fontWeight: '600'
    lineHeight: 16px
    letterSpacing: 0.06em
  caption:
    fontFamily: Inter
    fontSize: 12px
    fontWeight: '400'
    lineHeight: 16px
    letterSpacing: 0em
rounded:
  sm: 0.25rem
  DEFAULT: 0.5rem
  md: 0.75rem
  lg: 1rem
  xl: 1.5rem
  full: 9999px
spacing:
  gutter: 1rem
  gutter-tablet: 1.25rem
  gutter-desktop: 1.5rem
  margin: 1rem
  margin-tablet: 1.5rem
  margin-desktop: 2rem
  space-xs: 0.25rem
  space-sm: 0.5rem
  space-md: 1rem
  space-lg: 1.5rem
  space-xl: 2rem
---

## Brand & Style

This design system delivers an ultra-refined, architectural paradigm for human-AI interaction. Built on deep void darkness and sculptural micro-surfaces, the interface eliminates cognitive noise in favor of pure typographic precision and subtle physical presence. The emotional resonance is calm, authoritative, and perceptive—a high-end digital atelier that treats conversational intelligence as a focused creative instrument rather than a noisy chat feed.

The design movement combines **Corporate Modern** rigor with **Tactile Minimalism** and **Controlled 3D Depth**. Natural materials like milled obsidian, smoked glass, and micro-chamfered edges guide the digital textures. Interactive focal points—such as the 3D bubble cursor—introduce organic fluidity without compromising the system's geometric order.

## Colors

The color palette is strictly monochromatic and luminosity-driven, engineered for deep OLED efficiency, reduced optical strain, and high typographical clarity.

- **Canvas & Void (`#000000`)**: The deepest root frame and outer boundary.
- **Root Surface (`#09090b`)**: The primary conversation floor.
- **Secondary Containers (`#121215`)**: Recessed navigation drawers, tool headers, and collapsed panels.
- **Elevated Surfaces (`#18181b`)**: Active user speech bubbles, floating prompt docks, and contextual popovers.
- **Resting Hairlines (`#27272a`)**: Structural boundaries, dividers, and resting element borders.
- **Active Accents (`#3f3f46` to `#52525b`)**: Focused control outlines, active segmented states, and interactive hover rings.
- **High-Contrast Primary (`#fafafa`)**: Primary interactive affordances and key headlines.
- **Muted Typography (`#a1a1aa` and `#71717a`)**: Secondary guidance, timestamps, and tertiary parameter readouts.

## Typography

Typography relies on **Inter** for its neutral geometry, structural clarity, and uniform texture across dark backgrounds. Technical outputs, inline tokens, and structured reasoning traces use **JetBrains Mono** to establish visual separation between natural prose and computational logic.

Tighter negative tracking on display scales maintains structural cohesion on high-resolution displays. Small captions and metadata use expanded letter spacing for quick scanning against low-luminance planes. Paragraphs are given deliberate vertical breath to ensure extended reading comfort.

## Layout & Spacing

The layout is built around a centralized, focus-first content column anchored to an 8px base rhythm. Conversational flow is prioritized within a primary max-width container of 768px, expanding up to 1120px when tool outputs, code sandboxes, or dual-pane artifact views are active.

- **Mobile (<640px)**: Single-column edge-to-edge layout with 16px lateral padding. The prompt input locks to the viewport base with safe-area spacing.
- **Tablet (640px–1024px)**: Fluid chat stream with a 20px margin, introducing collapsible navigation sidebars and slide-over tool panels.
- **Desktop (>1024px)**: Split-pane workspace. Persistent primary navigation on the left, centered conversational thread, and an optional right-aligned canvas for live code inspection or markdown document previews.

## Elevation & Depth

Depth is established through tonal layering, specular surface edges, and micro-diffused ambient occlusions rather than heavy drop-shadows.

1. **Substrate (Level 0)**: `#000000` base viewport canvas.
2. **Conversation Floor (Level 1)**: `#09090b` main stage with faint hairline separation.
3. **Elevated Panels & Bubbles (Level 2)**: `#18181b` with a 1px border in `#27272a`. Enhanced with an internal top-edge highlight (`box-shadow: inset 0 1px 0 0 rgba(255, 255, 255, 0.05)`).
4. **Floating Overlays & Prompt Docks (Level 3)**: `#121215` with `backdrop-filter: blur(24px)` and low-frequency shadow: `0 16px 48px -12px rgba(0, 0, 0, 0.85), 0 0 0 1px #27272a`.
5. **Interactive 3D Cursor**: A dynamic 32px spherical glass orb rendered with a specular radial gradient: `radial-gradient(circle at 35% 35%, rgba(255, 255, 255, 0.8) 0%, rgba(255, 255, 255, 0.12) 40%, transparent 80%)`. The cursor responds with physics-driven spring mechanics, expanding and magnetically snapping over clickable controls.

## Shapes

The interface uses a calibrated rounded geometry (Level 2) that softens strict technical grids into tactile digital objects.

- Base containers, cards, and modal dialogs adopt an 8px to 16px radius (`0.5rem` to `1rem`).
- Prompt docks, suggestion chips, model selectors, and primary buttons use fully continuous pill boundaries (`rounded-full`) to emphasize their interactive state.
- Conversational speech bubbles feature asymmetric radii: user bubbles carry a 16px radius with a tighter 4px corner on the anchoring trailing edge, mirroring tactile speech balloons.

## Components

### Buttons & Interactive Controls
- **Primary Action**: Pill-shaped capsule filled with solid `#fafafa`, displaying `#09090b` bold typography. Micro-scales (`scale(1.02)`) on hover with a faint white ambient halo.
- **Secondary / Surface**: `#18181b` fill with a 1px `#27272a` boundary and `#f4f4f5` text. On hover, the border brightens to `#3f3f46` and the surface shifts to `#202024`.
- **Icon Actions**: 36px or 40px circular touch targets with transparent backgrounds, activating a `#18181b` background and `#ffffff` glyph color upon pointer entry.

### Suggestion Chips
- Floating query pills styled with `#121215` background, `#27272a` border, and `#a1a1aa` typography. Magnetically attract the 3D bubble cursor on hover, transitioning text to `#fafafa` and the border to `#3f3f46`.

### Inputs & Floating Prompt Dock
- Centered dock elevated 24px above the viewport bottom. 
- Composed of `#121215` surface, 24px frosted blur, and a continuous 1px `#27272a` border.
- Active typing states illuminate the perimeter to `#3f3f46` with an inner specular gradient. Includes inline micro-actions: paperclip attachment trigger, audio waveform indicator, and a circular solid-white send button.

### Chat Containers & Bubbles
- **User Message**: Right-aligned, resting on `#18181b` with a hairline `#27272a` outline, asymmetric corners, and `#fafafa` typography.
- **Assistant Stream**: Left-aligned, integrated directly into the `#09090b` conversation floor without boundary confinement. Long-form typography maintains a 1.625 line-height ratio for high legibility.
- **Reasoning / Thought Traces**: Collapsible accordion pill with a pulsing `#27272a` hairline border, dim `#121215` interior, and an animated shimmer rule during generation.

### Checkboxes, Radios & Switches
- Monochromatic toggle controls. Checkbox frames utilize a 1px `#3f3f46` outline on `#121215`, transitioning on selection to a solid `#fafafa` surface with a crisp dark `#09090b` checkmark glyph.
