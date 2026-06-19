# Design System Specification: Wetzel CRM
**Version:** 2.0  
**Date:** June 18, 2026  
**Status:** Applied  

---

## 1. Vision & Design Principles

The Wetzel CRM design system transforms a functional, utility-first application into an elegant, high-velocity executive cockpit. It blends the sleek, high-contrast clarity of modern developer tools with the premium, professional feel of enterprise platforms like OneSignal.

* **Integrated Continuity:** Elements do not sit on top of the interface; they emerge from it. The brand logo, navigation sidebar, and workspace blend together seamlessly using a unified surface layer paradigm.
* **Tactile Sophistication:** Moving away from sharp, rigid edges to modern, soft radiuses (12px to 16px) that give elements a defined, organic shape.
* **Information Hierarchy via Depth:** Substituting harsh borders with soft, deliberate drop shadows and subtle tonal variations to establish clear layering and direct user focus.
* **High-Intent Typography:** Utilizing clean, geometric sans-serif fonts with generous tracking for technical data, balanced by structured, high-contrast weights for headings and core metrics.

---

## 2. Unified Color Palette

The updated color foundation replaces generic off-whites with a premium, slate-tinted canvas. This palette offers depth, prevents visual fatigue, and emphasizes core sales metrics.

### 2.1 Core Surfaces
* **Application Canvas (Background):** `#F4F6F8` — A crisp, ultra-light slate grey that eliminates pure white glare and separates the main content from secondary windows.
* **Primary Surface Layer:** `#FFFFFF` — Used for active cards, workspaces, and primary content focus regions.
* **Navigation & Structural Sidebar:** `#0F172A` (Deep Slate Blue) to `#1E293B` (Mid-Slate) — A solid, dark foundational sidebar that allows the glowing nodes of the Wetzel logo to blend naturally into the UI.

### 2.2 Functional Accents
* **Brand Interactive Blue:** `#3B82F6` — Used for primary calls-to-action, selection states, and focus indicators.
* **Success Green:** `#10B981` — Clean, desaturated green for completed tasks, positive trends, and hitting OKR targets.
* **Opportunity Alert/High Priority:** `#EF4444` — Reserved exclusively for critical, high-scoring opportunities or overdue pipeline items.
* **Subtle Border/Muted Line:** `#E2E8F0` — Used sparingly for low-contrast structural boundaries.

---

## 3. Typography & Typeset Scale

Inspired by premium engineering and analytics dashboards, the typography emphasizes structure, readability, and clean data presentation.

* **Primary Typeface:** `Inter` or `Plus Jakarta Sans` (Clean, geometric neo-grotesque sans-serif).
* **System Monospace (Metrics & Scores):** `JetBrains Mono` or `SF Mono` (Used for precise numerical alignments like pipeline scores and OKR counts).

### Hierarchy Scale

| Level       | Size  | Weight | Tracking | Purpose                                        |
|-------------|-------|--------|----------|------------------------------------------------|
| Display 1   | 32pt  | 700    | -0.02em  | Large KPI Metric Numbers (e.g., 11,068)        |
| Heading 1   | 20pt  | 600    | -0.01em  | Page-level titles (e.g., Dashboard)            |
| Heading 2   | 14pt  | 600    | 0.05em   | Card Headers / Section Titles (All-Caps alt)   |
| Body Primary| 10pt  | 400    | 0.00em   | Standard interface data, forms, list items     |
| Body Muted  | 9pt   | 400    | 0.01em   | Labels, secondary metadata, timestamps         |

---

## 4. Modern Interface Elements & Component Styling

### 4.1 Surface Cards (The "Rounded Glass" Paradigm)
Cards must abandon thin, full-perimeter borders in favor of soft corners and soft depth shadows.

```css
.crm-card {
    background-color: #FFFFFF;
    border-radius: 16px;
    border: 1px solid rgba(226, 232, 240, 0.8);
    box-shadow: 0 4px 6px -1px rgba(15, 23, 42, 0.03), 
                0 2px 4px -2px rgba(15, 23, 42, 0.02);
    padding: 20px;
}
```

### 4.2 Interactive Buttons & Badges
Buttons use a distinct border-radius that mirrors the main container shapes, ensuring a consistent design language.

* **Primary Action Button:** Solid `#3B82F6` with white text. High contrast, explicit focus ring (`focus-ring: 2px #93C5FD`).
* **Secondary/Ghost Action:** Transparent background with `#64748B` text, shifting to a muted fill on hover (`#F1F5F9`).
* **Numerical Point Badges (e.g., Pipeline Scores):** Pill-shaped containers with a semi-transparent background tint matching the priority level.
  * High Priority (200+ pts): Background `rgba(59, 130, 246, 0.1)`, Text `#2563EB` (Deep Blue).
  * Standard Pipeline: Background `rgba(100, 116, 139, 0.1)`, Text `#475569`.

### 4.3 Iconography Strategy
Moving away from multi-colored, legacy emoticons toward sharp, minimalist vector line paths (inspired by OneSignal's unified iconography suite).

* **Style:** Thin-line geometric vectors (e.g., Lucide Icons scale), hard-coded to a uniform stroke weight (`stroke-width: 2px`).
* **Color-Coding:** Icons inherit the color state of their immediate parent container. A sidebar icon defaults to `#94A3B8` (Muted Blue-Gray) and transitions smoothly to active white `#FFFFFF` or brand blue `#3B82F6` upon selection.

---

## 5. Page Architecture & Structural Layout

The dashboard space is split into two functional zones: a persistent control anchor on the left and a fluid workspace on the right.

```
+-----------------------------------------------------------------------------------+
|  WETZEL LOGO (Blended) |  Dashboard  [Breadcrumb > Path]          User Profile (A) |
|  --------------------  |  ------------------------------------------------------  |
|  [i] Dashboard     (>) |  +-------------------+ +-------------------+ +--------+  |
|  [o] Agents            |  |  Open Ops: 4      | | Active To-Dos: 0  | | LinkedIn |  |
|  [target] Pipelines    |  +-------------------+ +-------------------+ +--------+  |
|  [check] To-Dos        |                                                          |
|  [users] Contacts      |  +---------------------------------+ +----------------+  |
|  [chart] OKRs          |  | Top 10 To-Dos                   | | Top 5 Ops      |  |
|                        |  |                                 | |                |  |
|                        |  |      [ Minimalist Vector ]      | | Dust  [160pts] |  |
|                        |  |       No active to-dos          | | QC    [150pts] |  |
|  [out] Logout          |  +---------------------------------+ +----------------+  |
+------------------------+----------------------------------------------------------+
```

### 5.1 The Blended Sidebar Container
The logo space in the upper left corner should not sit inside an isolated square box. By choosing a dark theme base color (`#0F172A`), the dark canvas of the Wetzel logo artwork integrates directly into the frame.

* **Width:** Fixed `260px` sidebar layout.
* **Interactive Row Elements:** Left-aligned minimalist icons, followed by a `12px` padding buffer, leading into the text. The right edge features an implicit chevron `>` pointer that fades into view only when hovering over that specific row item.

### 5.2 Main Content Viewport

* **Grid Structure:** Dynamic flex columns overlaying a 12-column layout grid system.
* **Spacing & Gaps:** Uniform `24px` margins between major workspace cards prevent layouts from feeling cluttered. This spacing ensures dense executive data remains highly readable.
* **Header Navigation Bar:** A clean, horizontal bar running along the top of the content area. It contains breadcrumb navigation paths on the left side, with user profile management tools grouped on the right.

---

## 6. v2.0 Refinements — OneSignal + Linear Inspiration

Applied June 18, 2026. Raises border radii, softens data chrome, and brings nav closer to Linear's density with OneSignal's clean action patterns.

### 6.1 Updated Radius Scale

| Token | v1.0 | v2.0 | Used For |
|---|---|---|---|
| `--wz-radius-sm` | 6px | 8px | Nav items, tags |
| `--wz-radius-md` | 8px | 10px | Form inputs |
| `--wz-radius-lg` | 14px | 16px | Cards, sections |
| `--wz-radius-xl` | 16px | 20px | Modals, overlays |

### 6.2 Navigation (Linear-Inspired)
Nav items occupy the full sidebar width with `8px` radius. Spacing is tighter (`1px` margin between items, `8px` horizontal padding). The active state uses `rgba(59,130,246,0.18)` — visible but not harsh on dark backgrounds.

### 6.3 Buttons (OneSignal-Inspired)
All `.btn` and `.btn-sm` elements use `border-radius: 9999px` (pill). Primary buttons gain a colored drop shadow (`rgba(59,130,246,0.28)`) that intensifies on hover. Secondary buttons use a muted gray fill rather than outlined.

### 6.4 Section Headers
Bottom borders removed from section headers. Title weight increased to `700`, letter-spacing `-0.01em`. "View All" action links styled as small pill badges (`rgba(59,130,246,0.10)` background).

### 6.5 Score Badges
Opportunity score badges changed from solid fill to tinted pill:
* Standard: `rgba(59,130,246,0.10)` bg, `#2563EB` text
* High (200+ pts): `rgba(16,185,129,0.10)` bg, `#059669` text
* Critical: `rgba(239,68,68,0.08)` bg, `#DC2626` text

### 6.6 Contact Cards
Left-border accent removed. Full `16px` rounded card with `box-shadow: var(--wz-shadow-1)`. Hover lifts with `translateY(-2px)` and a subtle blue border tint.

### 6.7 Opportunity Grid Table
Column headers changed from solid accent fill to muted `#F4F6F8` background with uppercase `11px` labels in `#64748B`. Eliminates the harsh blue header bar while maintaining clear structure.

### 6.8 Form Inputs
Radius updated to `10px`. Default fill uses canvas `#F4F6F8`; on focus transitions to white with a `3px rgba(59,130,246,0.10)` focus ring.
