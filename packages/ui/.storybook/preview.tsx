import "@fontsource/ibm-plex-sans/400.css";
import "@fontsource/ibm-plex-sans/600.css";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/600.css";
import type { Decorator, Preview } from "@storybook/react-vite";
import "../src/theme.css";
import { applyTheme } from "../src/theme";

// Every story renders in the theme picked in the toolbar (Office / Rig).
const withTheme: Decorator = (Story, ctx) => {
  applyTheme(ctx.globals.theme === "rig" ? "rig" : "office");
  return (
    <div style={{ background: "var(--bg)", color: "var(--text)", padding: 24, minHeight: "100vh" }}>
      <Story />
    </div>
  );
};

const preview: Preview = {
  decorators: [withTheme],
  globalTypes: {
    theme: {
      description: "Theme",
      toolbar: { title: "Theme", icon: "contrast", items: ["office", "rig"], dynamicTitle: true },
    },
  },
  initialGlobals: { theme: "office" },
  parameters: { a11y: { test: "error" }, layout: "fullscreen" },
};
export default preview;
