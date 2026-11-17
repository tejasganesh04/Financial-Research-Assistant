import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { "/v1": "http://127.0.0.1:8000" },
  },
  build: {
    rolldownOptions: {
      output: {
        // The core libraries change rarely: keep them in one long-cached file. Everything else,
        // including the Markdown parser used only for answers, is split where it is first used.
        advancedChunks: {
          groups: [
            {
              name: "vendor",
              test: /node_modules[\\/](react|react-dom|scheduler|axios)[\\/]/,
            },
          ],
        },
      },
    },
  },
});
