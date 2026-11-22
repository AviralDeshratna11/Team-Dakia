import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
// API_PORT lets a second API run beside another local copy (default 8000).
const api = `http://127.0.0.1:${process.env.API_PORT ?? 8000}`;
export default defineConfig({ plugins: [react(), tailwindcss()], server: { proxy: { '/api': api } } });
