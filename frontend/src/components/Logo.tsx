interface Props {
  size?: number
  className?: string
}

/**
 * Cleanplex mark: a "skip" bar beside a play triangle, in the app's amber
 * gradient. Kept as an inline SVG so it scales cleanly and needs no asset
 * pipeline.
 */
export default function Logo({ size = 36, className = '' }: Props) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
      aria-hidden="true"
    >
      <defs>
        <linearGradient id="cleanplex-gradient" x1="10" y1="10" x2="56" y2="56" gradientUnits="userSpaceOnUse">
          <stop stopColor="#ffc45c" />
          <stop offset="0.55" stopColor="#f0a500" />
          <stop offset="1" stopColor="#c47a00" />
        </linearGradient>
      </defs>
      <rect width="64" height="64" rx="16" fill="#11151a" />
      <rect x="1" y="1" width="62" height="62" rx="15" stroke="url(#cleanplex-gradient)" strokeOpacity="0.45" />
      <path d="M25 18.5v27L46 32 25 18.5Z" fill="url(#cleanplex-gradient)" />
      <path d="M18 18.5v27" stroke="#f7f8fa" strokeWidth="4" strokeLinecap="round" opacity="0.9" />
    </svg>
  )
}
