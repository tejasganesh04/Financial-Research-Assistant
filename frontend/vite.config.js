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
        // Libraries change rarely: keep them in their own long-cached files.
        advancedChunks: {
          groups: [
            {
              name: "markdown",
              test: /node_modules[\\/](react-markdown|remark|rehype|micromark|mdast|hast|unified|unist|vfile)/,
            },
            { name: "vendor", test: /node_modules/ },
          ],
        },
      },
    },
  },
});
