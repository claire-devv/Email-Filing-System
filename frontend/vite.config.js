import { defineConfig } from 'vite'
import react, { reactCompilerPreset } from '@vitejs/plugin-react'
import babel from '@rolldown/plugin-babel'

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    react(),
    babel({ presets: [reactCompilerPreset()] })
  ],
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8088',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, '')
      },
      '/auth': {
        target: 'http://127.0.0.1:8088',
        changeOrigin: true
      },
      '/emails': {
        target: 'http://127.0.0.1:8088',
        changeOrigin: true
      },
      '/activity': {
        target: 'http://127.0.0.1:8088',
        changeOrigin: true
      },
      '/review': {
        target: 'http://127.0.0.1:8088',
        changeOrigin: true
      },
      '/documents': {
        target: 'http://127.0.0.1:8088',
        changeOrigin: true
      },
      '/entities': {
        target: 'http://127.0.0.1:8088',
        changeOrigin: true
      },
      '/notifications': {
        target: 'http://127.0.0.1:8088',
        changeOrigin: true
      },
      '/admin': {
        target: 'http://127.0.0.1:8088',
        changeOrigin: true
      },
      '/users': {
        target: 'http://127.0.0.1:8088',
        changeOrigin: true
      }
    }
  }
})
