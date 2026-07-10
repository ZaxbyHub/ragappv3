/** @type {import('tailwindcss').Config} */
export default {
  darkMode: ["class"],
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    container: {
      center: true,
      padding: "2rem",
      screens: {
        "2xl": "1400px",
      },
    },
    extend: {
      /*
       * Material 3-inspired Theme Configuration
       * Warm neutral palette with custom fonts and animations
       */
      fontFamily: {
        sans: ['Spline Sans', 'system-ui', 'sans-serif'],
      },
      // Named micro-text token for the de-facto text-[10px] scale used ~49x
      // across the app (UI-VIS-6, #291). Centralizes the smallest text size.
      fontSize: {
        '2xs': ['0.625rem', { lineHeight: '0.9rem' }],
      },
      colors: {
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
        },
        secondary: {
          DEFAULT: "hsl(var(--secondary))",
          foreground: "hsl(var(--secondary-foreground))",
        },
        destructive: {
          DEFAULT: "hsl(var(--destructive))",
          foreground: "hsl(var(--destructive-foreground))",
        },
        success: {
          DEFAULT: "hsl(var(--success))",
          foreground: "hsl(var(--success-foreground))",
        },
        "success-subdued": {
          DEFAULT: "hsl(var(--success-subdued))",
          foreground: "hsl(var(--success-subdued-foreground))",
        },
        warning: {
          DEFAULT: "hsl(var(--warning))",
          foreground: "hsl(var(--warning-foreground))",
        },
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
        },
        accent: {
          DEFAULT: "hsl(var(--accent))",
          foreground: "hsl(var(--accent-foreground))",
        },
        popover: {
          DEFAULT: "hsl(var(--popover))",
          foreground: "hsl(var(--popover-foreground))",
        },
        card: {
          DEFAULT: "hsl(var(--card))",
          foreground: "hsl(var(--card-foreground))",
        },
      },
      borderRadius: {
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
      },
      keyframes: {
        /* Page load animation - fade in with slight upward movement */
        "page-load": {
          "0%": { 
            opacity: "0",
            transform: "translateY(10px)"
          },
          "100%": { 
            opacity: "1",
            transform: "translateY(0)"
          },
        },
        /* Stagger animation for list items */
        "stagger": {
          "0%": { 
            opacity: "0",
            transform: "translateY(20px) scale(0.98)"
          },
          "100%": { 
            opacity: "1",
            transform: "translateY(0) scale(1)"
          },
        },
        /* Material 3 ripple effect */
        "ripple": {
          "0%": {
            transform: "scale(0)",
            opacity: "0.5"
          },
          "100%": {
            transform: "scale(4)",
            opacity: "0"
          },
        },
        /* Subtle pulse for loading states */
        "pulse-soft": {
          "0%, 100%": {
            opacity: "1"
          },
          "50%": {
            opacity: "0.7"
          },
        },
        /* Slide in from bottom */
        "slide-up": {
          "0%": {
            transform: "translateY(100%)",
            opacity: "0"
          },
          "100%": {
            transform: "translateY(0)",
            opacity: "1"
          },
        },
      },
      animation: {
        "page-load": "page-load 0.5s ease-out forwards",
        "stagger": "stagger 0.4s ease-out forwards",
        "ripple": "ripple 0.6s ease-out forwards",
        "pulse-soft": "pulse-soft 2s ease-in-out infinite",
        "slide-up": "slide-up 0.3s ease-out forwards",
      },
    },
  },
  plugins: [require("tailwindcss-animate"), require("@tailwindcss/typography")],
}
