import type { Config } from "tailwindcss";
import preset from "../../packages/ui/tailwind-preset";

export default {
  presets: [preset as Config],
  content: ["./index.html", "./src/**/*.{ts,tsx}", "../../packages/ui/src/**/*.{ts,tsx}"],
} satisfies Config;
