interface MeridianLogoProps {
  className?: string;
  size?: number;
}

export function MeridianLogo({ className, size = 200 }: MeridianLogoProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 200 200"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
      aria-hidden="true"
      focusable="false"
    >
      <defs>
        {/* Left outer face — pink → purple */}
        <linearGradient
          id="leftOuter"
          gradientUnits="userSpaceOnUse"
          x1="70"
          y1="12"
          x2="54"
          y2="140"
        >
          <stop offset="0%" stopColor="#ff5ec8" />
          <stop offset="100%" stopColor="#7b61ff" />
        </linearGradient>

        {/* Left inner face — lavender → deep purple */}
        <linearGradient
          id="leftInner"
          gradientUnits="userSpaceOnUse"
          x1="70"
          y1="12"
          x2="100"
          y2="188"
        >
          <stop offset="0%" stopColor="#d4bfff" />
          <stop offset="100%" stopColor="#6c5ce7" />
        </linearGradient>

        {/* Right inner face — cyan → blue */}
        <linearGradient
          id="rightInner"
          gradientUnits="userSpaceOnUse"
          x1="130"
          y1="12"
          x2="100"
          y2="188"
        >
          <stop offset="0%" stopColor="#5ce1ff" />
          <stop offset="100%" stopColor="#0088ff" />
        </linearGradient>

        {/* Right outer face — light cyan → bright cyan */}
        <linearGradient
          id="rightOuter"
          gradientUnits="userSpaceOnUse"
          x1="130"
          y1="12"
          x2="146"
          y2="140"
        >
          <stop offset="0%" stopColor="#a8f0ff" />
          <stop offset="100%" stopColor="#00d9ff" />
        </linearGradient>
      </defs>

      {/* Left outer face */}
      <polygon points="70,12 8,92 100,188" fill="url(#leftOuter)" />

      {/* Left inner face */}
      <polygon points="70,12 100,55 100,188" fill="url(#leftInner)" />

      {/* Right inner face */}
      <polygon points="130,12 100,55 100,188" fill="url(#rightInner)" />

      {/* Right outer face */}
      <polygon points="130,12 192,92 100,188" fill="url(#rightOuter)" />
    </svg>
  );
}
