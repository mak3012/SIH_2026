import type { Config } from "tailwindcss";
import preset from "./tailwind-preset";

// Storybook build of the design system; apps/web has its own config with the same preset.
export default {
  presets: [preset as Config],
  content: ["./src/**/*.{ts,tsx}", "./stories/**/*.{ts,tsx}", "./.storybook/**/*.{ts,tsx}"],
} satisfies Config;
