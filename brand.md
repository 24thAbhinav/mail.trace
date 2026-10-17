# Brand — mail.trace

_Status: active_

## Direction

**Monochrome — soft black & white only.** No hue anywhere in the UI. Hierarchy
comes from tone, weight, contrast, and spacing rather than color. The closest
reference feel is a calm, modern SaaS console (Linear / Vercel / Height family)
restricted to a single neutral grayscale ramp.

One neutral temperature only (true neutral — no blue or brown tint). Mixing
temperatures reads as muddy.

## Palette

All values are CSS custom properties in `frontend/src/index.css`. Components
must use the semantic tokens, never raw hex.

### Ramp (neutral)

| Token | Light | Dark |
|---|---|---|
| `--gray-50` | `#fafafa` | — |
| `--gray-100` | `#f5f5f5` | — |
| `--gray-200` | `#e8e8e8` | — |
| `--gray-300` | `#d4d4d4` | — |
| `--gray-400` | `#a3a3a3` | — |
| `--gray-500` | `#737373` | — |
| `--gray-600` | `#525252` | — |
| `--gray-700` | `#404040` | — |
| `--gray-800` | `#262626` | — |
| `--gray-900` | `#171717` | — |
| `--gray-950` | `#0a0a0a` | — |

### Semantic

| Token | Meaning |
|---|---|
| `--bg` | app background |
| `--surface` | cards, panels, sidebar |
| `--surface-2` | inset / secondary fills |
| `--border`, `--border-strong` | hairline separators |
| `--text`, `--text-muted`, `--text-subtle` | text emphasis tiers |
| `--primary`, `--primary-fg` | primary action (near-black in light, near-white in dark) |
| `--ring` | focus ring |
| `--chip-mid-*`, `--chip-strong-*`, `--chip-solid-*` | monotone status chips in three emphasis tiers |

Status is expressed by **emphasis**, not color: neutral → mid gray → strong
inverted → solid, mapped to pipeline progression.

## Typography

- **UI:** Inter (fallback: system UI stack)
- **Numbers / code / dates:** JetBrains Mono, `tabular-nums` so digits don't jitter

## Radius, border, shadow

- Radius: `10px` cards/panels, `8px` controls, `999px` pills.
- Borders are `1px` hairlines. Separately, shadows are subtle and only used on
  floating surfaces — not both heavy border and heavy shadow on the same element.
- Shadows follow the theme: cool near-black at low opacity.

## Motion

Micro-interactions only, `100–150ms`, `ease-out` entering / `ease-in` leaving.
Never `transition: all`. All motion is disabled under
`prefers-reduced-motion: reduce`.

## Voice

Concise, active, specific. "Mark as closed", not "Click here to close this
application". Empty and error states say what happened and the next action.
