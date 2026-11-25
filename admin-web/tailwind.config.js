/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        canvas: "#F3F8F5",
        card: "#FFFFFF",
        border: "#E3EBE6",
        ink: "#171A18",
        muted: "#74807A",
        accent: "#15803D",
        "accent-deep": "#15803D",
        success: "#15803D",
        warning: "#D97706",
        danger: "#DC2626",
      },
      borderRadius: {
        panel: "32px",
        card: "24px",
        tile: "18px",
        control: "12px",
        chip: "8px",
        field: "24px",
        keycap: "6px",
      },
      maxWidth: {
        content: "1440px",
      },
      boxShadow: {
        soft: "0 1px 0 rgba(20,40,30,0.05)",
        dock: "0 18px 50px rgba(20,40,30,0.18), 0 6px 18px rgba(20,40,30,0.10)",
      },
      fontSize: {
        "13": ["13px", "20px"],
        "28": ["28px", "36px"],
      },
      fontFamily: {
        sans: [
          "-apple-system",
          "BlinkMacSystemFont",
          "Segoe UI",
          "PingFang SC",
          "Hiragino Sans GB",
          "Microsoft YaHei",
          "sans-serif",
        ],
        mono: [
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "Monaco",
          "Consolas",
          "Liberation Mono",
          "Courier New",
          "monospace",
        ],
      },
      keyframes: {
        enter: {
          "0%": {
            opacity: "var(--tw-enter-opacity,1)",
            transform:
              "translate3d(var(--tw-enter-translate-x,0),var(--tw-enter-translate-y,0),0) scale3d(var(--tw-enter-scale,1),var(--tw-enter-scale,1),var(--tw-enter-scale,1)) rotate(var(--tw-enter-rotate,0))",
          },
        },
        exit: {
          to: {
            opacity: "var(--tw-exit-opacity,1)",
            transform:
              "translate3d(var(--tw-exit-translate-x,0),var(--tw-exit-translate-y,0),0) scale3d(var(--tw-exit-scale,1),var(--tw-exit-scale,1),var(--tw-exit-scale,1)) rotate(var(--tw-exit-rotate,0))",
          },
        },
      },
      animation: {
        in: "enter 150ms ease-out",
        out: "exit 150ms ease-in",
      },
    },
  },
  plugins: [],
};
