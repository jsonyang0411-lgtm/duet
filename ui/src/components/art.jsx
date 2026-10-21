// Drawn in-house: the brand glyph and the placeholder figure for the assistant
// window. The real full-body art is still undecided (user, 2026-10-20); the
// placeholder keeps the same aspect ratio (435:1038) so it can be swapped later.

export function LogoGlyph() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M4 4h13l3 3v9h-8l-5 4v-4H4z" fill="currentColor" />
      <rect x="8" y="9" width="2.4" height="3.2" fill="var(--cy)" />
      <rect x="13.6" y="9" width="2.4" height="3.2" fill="var(--cy)" />
      <path d="M17 1.5v2.5" stroke="currentColor" strokeWidth="1.6" />
      <rect x="15.8" y="0.4" width="2.4" height="2.4" fill="currentColor" />
    </svg>
  )
}

export function FigurePlaceholder() {
  return (
    <svg viewBox="0 0 435 1038" preserveAspectRatio="xMidYMax meet" aria-hidden="true">
      <g className="fig-body">
        <path d="M128 300 L58 590 L96 604 L170 372 Z" />
        <path d="M307 300 L377 590 L339 604 L265 372 Z" />
        <path d="M150 702 L138 968 L198 968 L212 702 Z" />
        <path d="M223 702 L237 968 L297 968 L285 702 Z" />
        <path d="M118 296 Q217 246 317 296 L352 580 L334 714 L101 714 L83 580 Z" />
        <circle cx="217" cy="166" r="98" />
        <path d="M122 970 h84 v40 h-96 z M229 970 h84 l12 40 h-96 z" />
      </g>
    </svg>
  )
}
