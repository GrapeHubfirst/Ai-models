# Hosting This App With Browser Automation

The React website can be hosted for free on Vercel, Netlify, GitHub Pages, or Cloudflare Pages.

The browser automation relay cannot run inside a normal static host because Playwright needs a real server process and a browser profile. You have three practical options:

1. Static website + local relay

   Host the website on Vercel/Netlify and keep `python run.py` running on your computer. In Settings, set the relay URL to your tunnel URL.

   Free tunnel choices:

   ```bash
   cloudflared tunnel --url http://localhost:8765
   # or
   ngrok http 8765
   ```

2. Cheap/free server with browser

   Use Render, Railway, Fly.io, or a small VPS. Install Python, Playwright, and Chromium. This is less reliable on free tiers because browser automation uses RAM and long-running requests.

3. Chrome extension companion

   A browser extension can send page text to your relay or call `chrome.debugger`, but it still needs a local/native host if you want Python/Claude Code to edit files.

Recommended setup:

```text
Vercel/Netlify static site -> cloudflared tunnel -> local run.py -> ai_proxy.py/Claude Code
```

This keeps your cookies and Claude Code access on your own machine while still letting you open the website from anywhere.
