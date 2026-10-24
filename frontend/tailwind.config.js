/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        plex: {
          orange: '#f0a500',
          dark: '#12171d',
          darker: '#0d1116',
          bg: '#0b0f13',
          card: '#181e25',
          border: '#2a333f',
        },
      },
      fontFamily: {
        sans: [
          'Inter',
          'ui-sans-serif',
          'system-ui',
          '-apple-system',
          'BlinkMacSystemFont',
          '"Segoe UI"',
          'sans-serif',
        ],
      },
      boxShadow: {
        glow: '0 0 0 1px rgba(240,165,0,0.08), 0 18px 50px -24px rgba(0,0,0,0.9)',
      },
    },
  },
  plugins: [],
}
