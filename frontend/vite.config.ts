import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
// API_PORT lets a second API run beside another local copy (default 8000).
const api = `http://127.0.0.1:${process.env.API_PORT ?? 8000}`;
// Preact's React-compatible layer replaces React at bundle time (~10x smaller runtime).
// Source code and type-checking still use the React API and @types/react.
const preact = [
  { find: /^react-dom\/client$/, replacement: 'preact/compat/client' },
  { find: /^react-dom$/, replacement: 'preact/compat' },
  { find: /^react\/jsx-runtime$/, replacement: 'preact/jsx-runtime' },
  { find: /^react\/jsx-dev-runtime$/, replacement: 'preact/jsx-dev-runtime' },
  { find: /^react$/, replacement: 'preact/compat' },
];
// Load the main stylesheet without blocking first paint; the inline critical CSS in
// index.html covers the home page until it arrives (main.tsx waits for it).
const asyncCss = {
  name: 'async-css',
  enforce: 'post' as const,
  transformIndexHtml(html: string) {
    return html.replace(/<link rel="stylesheet"( crossorigin)? href="([^"]+)">/, (_m, co, href) =>
      `<link id="app-css" rel="preload" as="style"${co ?? ''} href="${href}" onload="this.onload=null;this.rel='stylesheet'"><noscript><link rel="stylesheet" href="${href}"></noscript>`);
  },
};
export default defineConfig({
  plugins: [react(), tailwindcss(), asyncCss],
  resolve: { alias: preact },
  build: { target: 'es2020', cssCodeSplit: false },
  server: { proxy: { '/api': api } },
});
