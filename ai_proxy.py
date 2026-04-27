#!/usr/bin/env python3
"""ai_proxy.py — Browser-automation proxy for free AI chats (no API keys).

Usage (called by run.py):
    python ai_proxy.py "your prompt" gemini

New features:
  - Multi-file parallel browser splitting: if a site only allows 1 file upload,
    files are split across N parallel browser instances and responses combined.
  - Perplexity full UI: weather, live data, file storage features properly enabled.
  - Memory system: persistent memory.json with user-controlled memories.
  - File storage: uploaded files saved to ./storage/ for reuse across sessions.

Supported models:
    gemini, chatgpt, perplexity, perplexity_connectors,
    lechat, chatai, arena, arena_battle, arena_direct, battle3, random,
    pollinations, flux, pixelbin

Requires: playwright (`pip install playwright && playwright install chromium`).
Cookies for each site are cached in ./cookies/ so logins persist between runs.
"""
import sys, time, os, json, io, random, base64, tempfile, mimetypes, shutil, hashlib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

SCRIPT_DIR   = Path(__file__).parent
COOKIES_DIR  = SCRIPT_DIR / "cookies";  COOKIES_DIR.mkdir(exist_ok=True)
STORAGE_DIR  = SCRIPT_DIR / "storage";  STORAGE_DIR.mkdir(exist_ok=True)
MEMORY_FILE  = SCRIPT_DIR / "memory.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")

# ── Stealth JS ───────────────────────────────────────────────────────────────

STEALTH_JS = """
() => {
  Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
  Object.defineProperty(navigator, 'plugins', {
    get: () => {
      const arr = [
        {name:'Chrome PDF Plugin',filename:'internal-pdf-viewer',description:'Portable Document Format'},
        {name:'Chrome PDF Viewer',filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai',description:''},
        {name:'Native Client',filename:'internal-nacl-plugin',description:''},
      ];
      arr.__proto__ = PluginArray.prototype;
      return arr;
    }
  });
  Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
  Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
  const origQuery = window.Permissions && window.Permissions.prototype.query;
  if (origQuery) {
    window.Permissions.prototype.query = function(params) {
      if (params && params.name === 'notifications') return Promise.resolve({state: 'default', onchange: null});
      return origQuery.call(this, params);
    };
  }
  if (!window.chrome) window.chrome = {};
  if (!window.chrome.runtime) window.chrome.runtime = {};
  const origIframe = HTMLIFrameElement.prototype.__lookupGetter__('contentWindow');
  if (origIframe) {
    Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
      get: function() {
        const w = origIframe.call(this);
        if (w && !w.chrome) { try { w.chrome = {runtime:{}}; } catch(e){} }
        return w;
      }
    });
  }
  const getParam = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function(param) {
    if (param === 37445) return 'Google Inc. (NVIDIA)';
    if (param === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)';
    return getParam.call(this, param);
  };
  delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
  delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
  delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
  Object.defineProperty(screen, 'width',       {get: () => 1920});
  Object.defineProperty(screen, 'height',      {get: () => 1080});
  Object.defineProperty(screen, 'availWidth',  {get: () => 1920});
  Object.defineProperty(screen, 'availHeight', {get: () => 1040});
  Object.defineProperty(screen, 'colorDepth',  {get: () => 24});
  Object.defineProperty(screen, 'pixelDepth',  {get: () => 24});
}
"""

LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--disable-dev-shm-usage",
    "--disable-gpu-sandbox",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-default-apps",
    "--disable-extensions-except=",
    "--window-size=1280,800",
    "--window-position=-32000,-32000",
    "--js-flags=--harmony",
]


# ── Memory system ────────────────────────────────────────────────────────────

def load_memory():
    """Load all memories from memory.json."""
    if not MEMORY_FILE.exists():
        return []
    try:
        with open(MEMORY_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_memory(memories):
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(memories, f, indent=2, ensure_ascii=False)


def add_memory(key, value):
    """Add or update a named memory."""
    memories = load_memory()
    for m in memories:
        if m.get("key") == key:
            m["value"] = value
            m["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            save_memory(memories)
            return
    memories.append({"key": key, "value": value, "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "updated": time.strftime("%Y-%m-%dT%H:%M:%S")})
    save_memory(memories)


def delete_memory(key):
    memories = load_memory()
    memories = [m for m in memories if m.get("key") != key]
    save_memory(memories)


def build_memory_context():
    """Build a memory preamble to prepend to prompts."""
    memories = load_memory()
    if not memories:
        return ""
    lines = ["[Your remembered context about this user:]"]
    for m in memories:
        lines.append(f"  - {m['key']}: {m['value']}")
    lines.append("")
    return "\n".join(lines) + "\n"


# ── File storage system ──────────────────────────────────────────────────────

def store_file(name, data_url):
    """Persist an uploaded file to ./storage/ for reuse. Returns path."""
    try:
        raw = split_data_url(data_url)
        safe = "".join(c for c in name if c.isalnum() or c in "._-") or "file"
        # Deduplicate by content hash
        h = hashlib.md5(raw).hexdigest()[:8]
        stem, ext = os.path.splitext(safe)
        final_name = f"{stem}_{h}{ext}"
        path = STORAGE_DIR / final_name
        if not path.exists():
            with open(path, "wb") as f:
                f.write(raw)
        return str(path)
    except Exception:
        return None


def list_stored_files():
    """Return metadata about files in ./storage/."""
    out = []
    for p in STORAGE_DIR.iterdir():
        if p.is_file():
            stat = p.stat()
            out.append({
                "name": p.name,
                "size": stat.st_size,
                "modified": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(stat.st_mtime)),
                "path": str(p),
            })
    return sorted(out, key=lambda x: x["modified"], reverse=True)


def load_stored_file_as_data_url(filename):
    """Load a stored file back as a data URL for re-uploading."""
    path = STORAGE_DIR / filename
    if not path.exists():
        return None
    typ = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    with open(path, "rb") as f:
        raw = f.read()
    b64 = base64.b64encode(raw).decode()
    return f"data:{typ};base64,{b64}"


# ── Cookies ──────────────────────────────────────────────────────────────────

def _cookie_path(name):
    return COOKIES_DIR / f"{name}.json"


def load_cookies(ctx, name):
    p = _cookie_path(name)
    if p.exists():
        try:
            ctx.add_cookies(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            pass


def save_cookies(ctx, name):
    try:
        _cookie_path(name).write_text(
            json.dumps(ctx.cookies(), ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


# ── Browser helpers ──────────────────────────────────────────────────────────

def launch(p):
    brave = "C:/Program Files/BraveSoftware/Brave-Browser/Application/brave.exe"
    if os.path.exists(brave):
        return p.chromium.launch(executable_path=brave, headless=False, args=LAUNCH_ARGS)
    return p.chromium.launch(headless=True, args=LAUNCH_ARGS)


def new_context(browser, name):
    ctx = browser.new_context(
        user_agent=UA,
        viewport={"width": 1280, "height": 800},
        screen={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="America/New_York",
        color_scheme="dark",
        java_script_enabled=True,
        accept_downloads=True,
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        },
    )
    ctx.add_init_script(STEALTH_JS)
    load_cookies(ctx, name)
    return ctx


def human_delay(lo=0.08, hi=0.22):
    time.sleep(random.uniform(lo, hi))


def paste_and_send(page, text):
    page.evaluate(f"navigator.clipboard.writeText({json.dumps(text)})")
    human_delay(0.3, 0.6)
    page.keyboard.press("Control+V")
    human_delay(0.3, 0.7)
    page.keyboard.press("Enter")


def find_input(page, selectors):
    for sel in selectors:
        el = page.query_selector(sel)
        if el:
            return el
    return None


# ── File helpers ─────────────────────────────────────────────────────────────

def load_request_files():
    path = os.environ.get("AI_PROXY_FILES")
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def split_data_url(data_url):
    if not data_url or "," not in data_url:
        return b""
    return base64.b64decode(data_url.split(",", 1)[1])


def append_text_file_context(prompt, files, limit=12000):
    chunks = []
    used = 0
    for f in files:
        name = f.get("name", "file")
        typ = f.get("type", "") or mimetypes.guess_type(name)[0] or ""
        if typ.startswith("image/"):
            continue
        try:
            raw = split_data_url(f.get("dataUrl", ""))
            text = raw.decode("utf-8", errors="replace")
            if not text.strip():
                continue
            remaining = max(0, limit - used)
            if remaining <= 0:
                break
            text = text[:remaining]
            used += len(text)
            chunks.append(f"\n\n--- Attached file: {name} ---\n{text}")
        except Exception:
            chunks.append(f"\n\n[Attached file: {name}]")
    return prompt + "".join(chunks)


def materialize_image_files(files):
    """Write image DataURLs to temp files so Playwright can upload them."""
    image_paths = []
    tmp_dir = None
    for f in files:
        name = f.get("name", "image.png")
        typ = f.get("type", "") or mimetypes.guess_type(name)[0] or ""
        if not typ.startswith("image/"):
            continue
        try:
            if tmp_dir is None:
                tmp_dir = tempfile.mkdtemp(prefix="aurora_uploads_")
            safe = "".join(c for c in name if c.isalnum() or c in "._-") or "image.png"
            path = os.path.join(tmp_dir, safe)
            with open(path, "wb") as out:
                out.write(split_data_url(f.get("dataUrl", "")))
            image_paths.append(path)
        except Exception:
            pass
    return tmp_dir, image_paths


def materialize_all_files(files):
    """Write ALL files (images + docs) to temp dir for upload."""
    paths = []
    tmp_dir = None
    for f in files:
        name = f.get("name", "file")
        try:
            if tmp_dir is None:
                tmp_dir = tempfile.mkdtemp(prefix="aurora_uploads_")
            safe = "".join(c for c in name if c.isalnum() or c in "._-") or "file"
            path = os.path.join(tmp_dir, safe)
            with open(path, "wb") as out:
                out.write(split_data_url(f.get("dataUrl", "")))
            paths.append(path)
        except Exception:
            pass
    return tmp_dir, paths


def cleanup_tmp_dir(tmp_dir):
    if tmp_dir and os.path.isdir(tmp_dir):
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


def upload_files_to_page(page, file_paths):
    """Try multiple strategies to upload files. Returns True if uploaded."""
    if not file_paths:
        return False
    for sel in [
        "input[type='file'][accept*='image']",
        "input[type='file'][multiple]",
        "input[type='file']",
    ]:
        try:
            inputs = page.query_selector_all(sel)
            if inputs:
                inputs[-1].set_input_files(file_paths)
                page.wait_for_timeout(2500)
                return True
        except Exception:
            pass
    for btn_sel in [
        "button[aria-label*='Attach']", "button[aria-label*='Upload']",
        "button:has-text('Attach')", "button:has-text('Add files')",
        "[data-testid*='attach']", "[class*='attach']",
    ]:
        try:
            btn = page.query_selector(btn_sel)
            if not btn:
                continue
            with page.expect_file_chooser(timeout=3000) as fc_info:
                btn.click()
            chooser = fc_info.value
            chooser.set_files(file_paths)
            page.wait_for_timeout(2500)
            return True
        except Exception:
            continue
    return False


def wait_stable(page, selector, timeout=90, stable_secs=2.5):
    start, last, stable_since = time.time(), "", None
    while time.time() - start < timeout:
        els = page.query_selector_all(selector)
        if els:
            try:
                t = els[-1].inner_text().strip()
            except Exception:
                t = ""
            if t and t != last:
                last, stable_since = t, time.time()
            elif t and stable_since and (time.time() - stable_since) >= stable_secs:
                return t
        time.sleep(0.4)
    return last or "(No response)"


def grab_widget_html(page, wrappers, prose_fallback=True):
    js_wrappers = json.dumps(wrappers)
    js = """(wrappers) => {
        const drop = 'script,style,[class*="action"],[class*="feedback"],[class*="share"],'
                   + '[class*="vote"],[class*="copy"],[class*="thumb"]';
        for (const sel of wrappers) {
            const els = document.querySelectorAll(sel);
            if (els.length) {
                const el = els[els.length - 1];
                const clone = el.cloneNode(true);
                clone.querySelectorAll(drop).forEach(e => e.remove());
                clone.querySelectorAll('img').forEach(i => {
                    const src = i.getAttribute('src') || i.getAttribute('data-src') || i.currentSrc || '';
                    if (src && !src.startsWith('data:')) {
                        try { i.setAttribute('src', new URL(src, location.href).href); } catch(e) {}
                    }
                    i.setAttribute('referrerpolicy','no-referrer');
                    i.setAttribute('crossorigin','anonymous');
                    i.removeAttribute('srcset');
                });
                const html = clone.innerHTML.trim();
                if (html.length > 100) return html;
            }
        }
        return null;
    }"""
    html = page.evaluate(f"({js})({js_wrappers})")
    if html:
        return "__HTML__:" + html
    if prose_fallback:
        text = page.evaluate(
            "() => {const els = document.querySelectorAll('div[class*=\"prose\"], .prose');"
            "return els.length ? els[els.length-1].innerText.trim() : '(no response)';}"
        )
        return text
    return "(no response)"


# ── Multi-file parallel browser splitting ────────────────────────────────────

def run_parallel_with_file_chunks(ask_fn, prompt, files, chunk_size=1, label="AI"):
    """
    Split files into chunks of chunk_size and call ask_fn concurrently,
    one browser instance per chunk. Combines all responses.
    If only 1 file or chunk_size >= len(files), falls back to single call.
    """
    if len(files) <= chunk_size:
        return ask_fn(prompt, files)

    chunks = [files[i:i+chunk_size] for i in range(0, len(files), chunk_size)]
    results = {}

    def run_chunk(idx, chunk):
        return idx, ask_fn(
            f"[Part {idx+1}/{len(chunks)} — analyzing file(s): {', '.join(f.get('name','?') for f in chunk)}]\n\n{prompt}",
            chunk
        )

    with ThreadPoolExecutor(max_workers=min(len(chunks), 4)) as ex:
        futures = {ex.submit(run_chunk, i, c): i for i, c in enumerate(chunks)}
        for fut in as_completed(futures, timeout=300):
            try:
                idx, r = fut.result()
                results[idx] = r
            except Exception as e:
                results[futures[fut]] = f"Error: {e}"

    parts = []
    for i in range(len(chunks)):
        r = results.get(i, "(no response)")
        if isinstance(r, str) and r.startswith("__HTML__:"):
            r = r[9:]
        file_names = ", ".join(f.get("name","?") for f in chunks[i])
        parts.append(f"**[{label} — File(s): {file_names}]**\n\n{r}")

    combined = "\n\n---\n\n".join(parts)
    if len(parts) > 1:
        combined = f"*{len(chunks)} parallel sessions combined:*\n\n" + combined
    return combined


# ── Gemini ───────────────────────────────────────────────────────────────────

def _ask_gemini_single(prompt, files=None):
    from playwright.sync_api import sync_playwright
    files = files or []
    mem_ctx = build_memory_context()
    prompt = mem_ctx + prompt
    prompt = append_text_file_context(prompt, files)
    tmp_dir, image_paths = materialize_image_files(files)

    # Also persist files to storage
    for f in files:
        store_file(f.get("name", "file"), f.get("dataUrl", ""))

    with sync_playwright() as p:
        browser = launch(p)
        ctx = new_context(browser, "gemini")
        page = ctx.new_page()
        try:
            page.goto("https://gemini.google.com/app", timeout=45000)
            page.wait_for_selector("rich-textarea, textarea", timeout=45000)
            ta = find_input(page, ["rich-textarea div[contenteditable='true']",
                                   "rich-textarea", "textarea"])
            if not ta:
                return "Error: Gemini input not found. Sign in once in Brave first."
            if image_paths:
                upload_files_to_page(page, image_paths)
                ta = find_input(page, ["rich-textarea div[contenteditable='true']", "rich-textarea", "textarea"])
            ta.click()
            time.sleep(0.3)
            paste_and_send(page, prompt)
            page.wait_for_selector("message-content .markdown", timeout=90000)
            time.sleep(2)
            wait_stable(page, "message-content .markdown", timeout=60, stable_secs=2.0)
            html = grab_widget_html(page, ["message-content", "model-response", "[class*='response-content']"])
            save_cookies(ctx, "gemini")
            return html
        except Exception as e:
            return f"Error: {e}"
        finally:
            cleanup_tmp_dir(tmp_dir)
            browser.close()


def ask_gemini(prompt):
    files = load_request_files()
    # Gemini supports multiple files natively; send all at once
    return _ask_gemini_single(prompt, files)


# ── ChatGPT ──────────────────────────────────────────────────────────────────

def _ask_chatgpt_single(prompt, files=None):
    from playwright.sync_api import sync_playwright
    files = files or []
    mem_ctx = build_memory_context()
    prompt = mem_ctx + prompt
    tmp_dir, all_paths = materialize_all_files(files)
    text_prompt = append_text_file_context(prompt, [f for f in files if not (f.get("type","") or "").startswith("image/")])

    for f in files:
        store_file(f.get("name", "file"), f.get("dataUrl", ""))

    with sync_playwright() as p:
        browser = launch(p)
        ctx = new_context(browser, "chatgpt")
        page = ctx.new_page()
        try:
            page.goto("https://chatgpt.com/", timeout=45000)
            page.wait_for_timeout(8000)
            ta = find_input(page, ["div.ProseMirror", "[contenteditable='true']", "textarea"])
            if not ta:
                return ("Error: ChatGPT input not found. The site likely needs a CAPTCHA "
                        "or login — open chatgpt.com in your browser once.")
            if all_paths:
                upload_files_to_page(page, all_paths)
                ta = find_input(page, ["div.ProseMirror", "[contenteditable='true']", "textarea"])
            ta.click()
            time.sleep(0.3)
            paste_and_send(page, text_prompt)
            time.sleep(3)
            wait_stable(page, "div[data-message-author-role='assistant']", timeout=90, stable_secs=2.5)
            html = grab_widget_html(page, [
                "div[data-message-author-role='assistant']:last-of-type",
                "div[data-message-author-role='assistant']",
                "article[data-testid*='conversation-turn']:last-of-type",
            ])
            save_cookies(ctx, "chatgpt")
            return html
        except Exception as e:
            return f"Error: {e}"
        finally:
            cleanup_tmp_dir(tmp_dir)
            browser.close()


def ask_chatgpt(prompt):
    files = load_request_files()
    if len(files) > 1:
        # ChatGPT allows multiple files; try all at once first, fall back to chunked
        return run_parallel_with_file_chunks(_ask_chatgpt_single, prompt, files, chunk_size=5, label="ChatGPT")
    return _ask_chatgpt_single(prompt, files)


# ── Perplexity (full UI: weather, live data, storage, files) ────────────────

def _ask_perplexity_single(prompt, files=None):
    """
    Full-featured Perplexity session:
    - Properly waits for the Sonar/web widgets (weather cards, live data) to render
    - Uploads files (images AND documents) via the attach button
    - Captures rich HTML including widget iframes and weather cards
    - Enables "storage" by keeping a persistent profile context (cookies)
    """
    from playwright.sync_api import sync_playwright
    files = files or []
    mem_ctx = build_memory_context()
    full_prompt = mem_ctx + prompt

    # Perplexity can handle images natively; embed text files in prompt
    image_files = [f for f in files if (f.get("type","") or mimetypes.guess_type(f.get("name",""))[0] or "").startswith("image/")]
    text_files  = [f for f in files if f not in image_files]
    full_prompt = append_text_file_context(full_prompt, text_files)

    tmp_dir, image_paths = materialize_image_files(files)
    for f in files:
        store_file(f.get("name","file"), f.get("dataUrl",""))

    with sync_playwright() as p:
        browser = launch(p)
        ctx = new_context(browser, "perplexity")
        page = ctx.new_page()
        try:
            page.goto("https://www.perplexity.ai/", timeout=45000)
            page.wait_for_timeout(6000)

            # Dismiss consent banners
            for label in ("Accept", "I agree", "Continue", "Got it", "OK"):
                btn = page.query_selector(f"button:has-text('{label}')")
                if btn:
                    try:
                        btn.click()
                        time.sleep(0.6)
                    except Exception:
                        pass

            input_selectors = [
                "div[role='textbox'][contenteditable='true']",
                "div[contenteditable='true'][data-lexical-editor='true']",
                "div[contenteditable='true']",
                "textarea[placeholder]",
                "textarea",
            ]
            ta = None
            for _ in range(10):
                ta = find_input(page, input_selectors)
                if ta:
                    break
                page.wait_for_timeout(800)
            if not ta:
                return ("Error: Perplexity input not found. Open https://www.perplexity.ai/ "
                        "in the browser once and accept any consent banners.")

            # Upload images if any
            if image_paths:
                uploaded = upload_files_to_page(page, image_paths)
                if not uploaded:
                    full_prompt += "\n\n[Image attachment upload failed — please describe the image if needed.]"
                ta = find_input(page, input_selectors) or ta

            try:
                ta.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass
            try:
                ta.click()
            except Exception:
                page.click("body")
                time.sleep(0.3)
                ta = find_input(page, input_selectors) or ta
                ta.click()
            time.sleep(0.3)

            try:
                page.keyboard.type(full_prompt, delay=4)
            except Exception:
                paste_and_send(page, full_prompt)
            time.sleep(0.4)

            sent = False
            for btn_sel in [
                "button[aria-label*='Submit']",
                "button[aria-label*='Send']",
                "button:has(svg[data-icon='arrow-right'])",
                "button[type='submit']",
            ]:
                try:
                    btn = page.query_selector(btn_sel)
                    if btn:
                        btn.click()
                        sent = True
                        break
                except Exception:
                    continue
            if not sent:
                page.keyboard.press("Enter")

            # Wait for answer to appear
            answer_selectors = [
                "div[id^='markdown-content']",
                "div[class*='prose']",
                "[class*='AnswerBody']",
                "[class*='ThreadItem']",
                "[data-testid*='answer']",
                ".prose",
            ]
            for sel in answer_selectors:
                try:
                    page.wait_for_selector(sel, timeout=15000)
                    break
                except Exception:
                    continue

            # Extra wait for live widgets (weather cards, maps, etc.) to load
            time.sleep(6)
            wait_stable(page, ", ".join(answer_selectors), timeout=120, stable_secs=3.0)

            # Additional widget-specific wait: weather, knowledge panels
            for widget_sel in [
                "[class*='WeatherCard']", "[class*='weather']",
                "[class*='KnowledgePanel']", "[class*='widget']",
                "iframe[src*='widget']",
            ]:
                try:
                    page.wait_for_selector(widget_sel, timeout=5000)
                    time.sleep(2)  # let it fully render
                    break
                except Exception:
                    pass

            # Grab the full rich HTML including any embedded widgets
            html = grab_widget_html(page, [
                "div[id^='markdown-content']",
                "[class*='AnswerBody']",
                "[class*='ThreadItem']",
                "[class*='Answer']",
                "[data-testid*='answer']",
                "section[class]",
            ])

            # Also try to grab any weather/widget cards that sit outside the answer div
            try:
                extra_widgets = page.evaluate("""() => {
                    const selectors = [
                        '[class*="WeatherCard"]', '[class*="weather-card"]',
                        '[class*="KnowledgeCard"]', '[class*="SportsCard"]',
                        '[class*="FinanceCard"]', '[class*="widget"]',
                        '[data-testid*="widget"]', '[data-testid*="card"]',
                    ];
                    const seen = new Set();
                    const parts = [];
                    for (const sel of selectors) {
                        document.querySelectorAll(sel).forEach(el => {
                            if (!seen.has(el)) {
                                seen.add(el);
                                const html = el.outerHTML;
                                if (html.length > 50) parts.push(html);
                            }
                        });
                    }
                    return parts.join('\\n');
                }""")
                if extra_widgets and len(extra_widgets) > 100:
                    if isinstance(html, str) and html.startswith("__HTML__:"):
                        html = html + "\n<!-- widgets -->\n" + extra_widgets
                    else:
                        html = "__HTML__:" + f"<div>{html}</div>\n<!-- widgets -->\n{extra_widgets}"
            except Exception:
                pass

            save_cookies(ctx, "perplexity")
            return html
        except Exception as e:
            return f"Error: {e}"
        finally:
            cleanup_tmp_dir(tmp_dir)
            browser.close()


def ask_perplexity(prompt):
    files = load_request_files()
    if len(files) > 1:
        # Perplexity allows 1 file per query on free tier; split across browsers
        return run_parallel_with_file_chunks(_ask_perplexity_single, prompt, files, chunk_size=1, label="Perplexity")
    return _ask_perplexity_single(prompt, files)


def ask_perplexity_connectors(prompt):
    return ask_perplexity(prompt)


# ── Mistral / Le Chat ────────────────────────────────────────────────────────

def _ask_lechat_single(prompt, files=None):
    from playwright.sync_api import sync_playwright
    files = files or []
    mem_ctx = build_memory_context()
    prompt = mem_ctx + append_text_file_context(prompt, files)
    tmp_dir, image_paths = materialize_image_files(files)
    for f in files:
        store_file(f.get("name","file"), f.get("dataUrl",""))

    with sync_playwright() as p:
        browser = launch(p)
        ctx = new_context(browser, "lechat")
        page = ctx.new_page()
        try:
            page.goto("https://chat.mistral.ai/chat", timeout=45000)
            page.wait_for_timeout(8000)
            for label in ("Accept", "I agree", "Continue", "Got it"):
                btn = page.query_selector(f"button:has-text('{label}')")
                if btn:
                    try:
                        btn.click()
                        time.sleep(1)
                    except Exception:
                        pass
            ta = find_input(page, ["textarea", "[contenteditable='true']"])
            if not ta:
                return "Error: Mistral input not found. Sign in once at chat.mistral.ai."
            if image_paths:
                upload_files_to_page(page, image_paths)
                ta = find_input(page, ["textarea", "[contenteditable='true']"])
            ta.click()
            time.sleep(0.3)
            paste_and_send(page, prompt)
            wait_stable(page, "div[class*='assistant']", timeout=90, stable_secs=2.5)
            html = grab_widget_html(page, [
                "div[class*='assistant']:last-of-type",
                "[class*='message'][class*='assistant']",
                "article",
            ])
            save_cookies(ctx, "lechat")
            return html
        except Exception as e:
            return f"Error: {e}"
        finally:
            cleanup_tmp_dir(tmp_dir)
            browser.close()


def ask_lechat(prompt):
    files = load_request_files()
    if len(files) > 1:
        return run_parallel_with_file_chunks(_ask_lechat_single, prompt, files, chunk_size=1, label="LeChat")
    return _ask_lechat_single(prompt, files)


# ── chat.z.ai ────────────────────────────────────────────────────────────────

def ask_chatai(prompt):
    from playwright.sync_api import sync_playwright
    files = load_request_files()
    mem_ctx = build_memory_context()
    prompt = mem_ctx + append_text_file_context(prompt, files)
    for f in files:
        store_file(f.get("name","file"), f.get("dataUrl",""))

    with sync_playwright() as p:
        browser = launch(p)
        ctx = new_context(browser, "chatai")
        page = ctx.new_page()
        try:
            page.goto("https://chat.z.ai/", timeout=45000)
            page.wait_for_timeout(12000)
            ta = find_input(page, ["textarea", "[contenteditable='true']"])
            if not ta:
                page.click("body")
                page.wait_for_timeout(2000)
                ta = find_input(page, ["textarea", "[contenteditable='true']"])
            if not ta:
                return "Error: z.ai input not found."
            ta.click()
            time.sleep(0.4)
            paste_and_send(page, prompt)
            for sel in ["article", "[class*='message']", "[class*='assistant']", ".prose"]:
                try:
                    page.wait_for_selector(sel, timeout=15000)
                    break
                except Exception:
                    continue
            time.sleep(3)
            text = wait_stable(page, "article, [class*='message'], .prose", timeout=90, stable_secs=2.5)
            save_cookies(ctx, "chatai")
            return text
        except Exception as e:
            return f"Error: {e}"
        finally:
            browser.close()


# ── Arena.ai ─────────────────────────────────────────────────────────────────

def ask_arena(prompt):
    from playwright.sync_api import sync_playwright
    mem_ctx = build_memory_context()
    prompt = mem_ctx + prompt
    with sync_playwright() as p:
        browser = launch(p)
        ctx = new_context(browser, "arena")
        page = ctx.new_page()
        try:
            page.goto("https://arena.ai/", timeout=45000)
            page.wait_for_timeout(8000)
            for label in ("Agree", "Accept", "Continue"):
                btn = page.query_selector(f"button:has-text('{label}')")
                if btn:
                    try:
                        btn.click()
                        time.sleep(1)
                    except Exception:
                        pass
            ta = find_input(page, ["textarea", "[contenteditable='true']"])
            if not ta:
                return "Error: arena.ai input not found."
            ta.click()
            time.sleep(0.4)
            paste_and_send(page, prompt)
            time.sleep(45)
            data = page.evaluate("""() => {
                const seen = new Set(); const out = [];
                document.querySelectorAll('div').forEach(d => {
                    const t = (d.innerText || '').trim();
                    if (t.length < 120 || t.length > 12000) return;
                    if (/Terms of Use|Privacy|Sign up|Login|Battle Mode|Add files/.test(t)) return;
                    const k = t.slice(0, 100);
                    if (!seen.has(k)) { seen.add(k); out.push(t); }
                });
                return out.slice(0, 2);
            }""")
            save_cookies(ctx, "arena")
            a = (data[0] if len(data) > 0 else "(no response 1)")[:5000]
            b = (data[1] if len(data) > 1 else "(no response 2)")[:5000]
            return f"[Model 1]\n\n{a}\n\n--- VS ---\n\n[Model 2]\n\n{b}"
        except Exception as e:
            return f"Error: {e}"
        finally:
            browser.close()




def ask_arena_battle(prompt):
    """Arena Battle Mode — uses lmarena.ai anonymous battle (no sign-up needed)."""
    from playwright.sync_api import sync_playwright
    mem_ctx = build_memory_context()
    prompt = mem_ctx + prompt
    with sync_playwright() as p:
        browser = launch(p)
        ctx = new_context(browser, "arena_battle")
        page = ctx.new_page()
        try:
            page.goto("https://lmarena.ai/?mode=battle", timeout=45000)
            page.wait_for_timeout(6000)
            # Accept any consent dialogs
            for label in ("Agree", "Accept", "Continue", "Got it", "OK"):
                btn = page.query_selector(f"button:has-text('{label}')")
                if btn:
                    try:
                        btn.click()
                        time.sleep(1)
                    except Exception:
                        pass
            # Find the chat input
            ta = find_input(page, ["textarea", "[contenteditable='true']", "input[type='text']"])
            if not ta:
                # Try direct chat URL
                page.goto("https://lmarena.ai/", timeout=30000)
                page.wait_for_timeout(4000)
                ta = find_input(page, ["textarea", "[contenteditable='true']"])
            if not ta:
                return "Error: lmarena.ai input not found. Visit lmarena.ai in your browser once."
            ta.click()
            time.sleep(0.4)
            paste_and_send(page, prompt)
            time.sleep(50)
            # Extract the two model responses
            data = page.evaluate("""() => {
                const seen = new Set(); const out = [];
                // Try to find battle response columns
                const cols = document.querySelectorAll('[class*="col"],[class*="model"],[class*="response"],[class*="answer"]');
                cols.forEach(col => {
                    const t = (col.innerText || '').trim();
                    if (t.length < 80 || t.length > 12000) return;
                    if (/Arena|Battle|Sign|Login|Terms|Privacy|Modal|Button/.test(t.slice(0,50))) return;
                    const k = t.slice(0, 80);
                    if (!seen.has(k)) { seen.add(k); out.push(t); }
                });
                if (out.length < 2) {
                    // Fallback: grab large text blocks
                    document.querySelectorAll('div,p').forEach(d => {
                        const t = (d.innerText || '').trim();
                        if (t.length < 100 || t.length > 12000) return;
                        if (/Arena|Battle Mode|Sign up|Login|Terms of Use|Privacy/.test(t)) return;
                        const k = t.slice(0, 80);
                        if (!seen.has(k)) { seen.add(k); out.push(t); }
                    });
                }
                return out.slice(0, 2);
            }""")
            save_cookies(ctx, "arena_battle")
            a = (data[0] if len(data) > 0 else "(no response 1)")[:5000]
            b = (data[1] if len(data) > 1 else "(no response 2)")[:5000]
            return f"[Model A — Anonymous]\n\n{a}\n\n--- VS ---\n\n[Model B — Anonymous]\n\n{b}"
        except Exception as e:
            return f"Error: {e}"
        finally:
            browser.close()

ARENA_DIRECT_MODELS = {
    "claude-sonnet-4-6-search": "claude-sonnet-4-6-search",
    "gpt-5.2-search": "gpt-5.2-search",
    "gemini-3-flash-grounding": "gemini-3-flash-grounding",
    "grok-4.20-multi-agent-beta-0309": "grok-4.20-multi-agent-beta-0309",
    "gpt-5.1-search": "gpt-5.1-search",
    "grok-4-1-fast-search": "grok-4-1-fast-search",
    "ppl-sonar-reasoning-pro-high": "ppl-sonar-reasoning-pro-high",
    "claude-sonnet-4-5-search": "claude-sonnet-4-5-search",
    "gpt-image-2-medium": "gpt-image-2 (medium)",
    "gemini-2.5-flash-image-preview (nano-banana)": "gemini-2.5-flash-image-preview",
    "imagen-4.0-fast-generate-001": "imagen-4.0-fast-generate-001",
    "veo-3.1-fast-audio-1080p": "veo-3.1-fast-audio-1080p",
    "sora-2": "sora-2",
}


def ask_arena_direct(prompt, arena_model="claude-sonnet-4-6-search"):
    from playwright.sync_api import sync_playwright
    mem_ctx = build_memory_context()
    prompt = mem_ctx + prompt
    target = ARENA_DIRECT_MODELS.get(arena_model, arena_model or "claude-sonnet-4-6-search")
    with sync_playwright() as p:
        browser = launch(p)
        ctx = new_context(browser, "arena_direct")
        page = ctx.new_page()
        try:
            page.goto("https://arena.ai/code/direct", timeout=45000)
            page.wait_for_timeout(8000)
            for label in ("Agree", "Accept", "Continue"):
                btn = page.query_selector(f"button:has-text('{label}')")
                if btn:
                    try:
                        btn.click(); time.sleep(1)
                    except Exception:
                        pass
            for sel in [
                "button:has-text('gemini')", "button:has-text('model')",
                "button[role='combobox']", "[role='combobox']",
                "button:has-text('Direct') + button",
            ]:
                try:
                    b = page.query_selector(sel)
                    if b:
                        b.click(); time.sleep(1)
                        break
                except Exception:
                    pass
            try:
                item = page.query_selector(f"text={target}") or page.query_selector(f"[role='option']:has-text('{target}')")
                if item:
                    item.click(); time.sleep(1)
            except Exception:
                pass
            ta = find_input(page, ["textarea", "[contenteditable='true']"])
            if not ta:
                return "Error: arena.ai direct input not found. Open arena.ai/code/direct once and sign in/accept terms."
            ta.click(); time.sleep(0.3)
            paste_and_send(page, prompt)
            time.sleep(5)
            wait_stable(page, "article, [class*='message'], [class*='assistant'], [class*='prose']", timeout=120, stable_secs=3)
            html = grab_widget_html(page, [
                "article:last-of-type",
                "[class*='assistant']:last-of-type",
                "[class*='message']:last-of-type",
                "[class*='prose']:last-of-type",
                "section[class]:last-of-type",
            ])
            save_cookies(ctx, "arena_direct")
            return html
        except Exception as e:
            return f"Error: {e}"
        finally:
            browser.close()


# ── 3-model battle ───────────────────────────────────────────────────────────

def ask_battle3(prompt):
    jobs = {
        "gemini": lambda p: _ask_gemini_single(p),
        "chatgpt": lambda p: _ask_chatgpt_single(p),
        "perplexity": lambda p: _ask_perplexity_single(p),
    }
    out = {}
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {ex.submit(fn, prompt): name for name, fn in jobs.items()}
        for fut in as_completed(futures, timeout=300):
            name = futures[fut]
            try:
                r = fut.result()
            except Exception as e:
                r = f"Error: {e}"
            if isinstance(r, str) and r.startswith("__HTML__:"):
                r = r[9:]
            out[name] = r
    parts = [f"[{n.upper()}]\n\n{out.get(n,'(missing)')}" for n in ("gemini","chatgpt","perplexity")]
    return "__BATTLE3__: " + "\n\n".join(parts)


def ask_random(prompt):
    r = random.random()
    if r < 0.4:
        return ask_arena_battle(prompt)
    elif r < 0.7:
        return ask_arena(prompt)
    else:
        return ask_battle3(prompt)


# ── Image / Video ─────────────────────────────────────────────────────────────

def ask_pollinations(prompt):
    import urllib.parse
    encoded = urllib.parse.quote(prompt)
    seed = int(time.time())
    url = (f"https://image.pollinations.ai/prompt/{encoded}"
           f"?width=1024&height=768&nologo=true&seed={seed}")
    return f"![{prompt}]({url})\n\n[Open image]({url})"


def ask_flux(prompt):
    import urllib.parse
    encoded = urllib.parse.quote(prompt)
    seed = int(time.time())
    url = (f"https://image.pollinations.ai/prompt/{encoded}"
           f"?width=1024&height=768&model=flux&nologo=true&seed={seed}")
    return f"![{prompt}]({url})\n\n[Open image]({url})"


def ask_pixelbin(prompt):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = launch(p)
        ctx = new_context(browser, "pixelbin")
        page = ctx.new_page()
        try:
            page.goto("https://www.pixelbin.io/ai-tools/video-generator", timeout=45000)
            page.wait_for_timeout(8000)
            ta = find_input(page, ["textarea", "input[type='text']", "[contenteditable='true']"])
            if not ta:
                return "Error: Pixelbin input not found."
            ta.click()
            time.sleep(0.4)
            paste_and_send(page, prompt)
            try:
                page.wait_for_selector("video", timeout=180000)
                src = page.evaluate("() => { const v=document.querySelector('video'); return v?(v.src||v.currentSrc):null; }")
                if src:
                    return f"Video generated: {src}"
            except Exception:
                pass
            return "Video generation started — check the browser window for the result."
        except Exception as e:
            return f"Error: {e}"
        finally:
            browser.close()


# ── Memory management commands ────────────────────────────────────────────────

def handle_memory_command(args):
    """Handle memory management from command line: memory list|add|delete|clear"""
    if not args:
        memories = load_memory()
        return json.dumps(memories, indent=2, ensure_ascii=False)
    cmd = args[0].lower()
    if cmd == "list":
        memories = load_memory()
        return json.dumps(memories, indent=2, ensure_ascii=False)
    elif cmd == "add" and len(args) >= 3:
        add_memory(args[1], " ".join(args[2:]))
        return f"Memory added: {args[1]}"
    elif cmd == "delete" and len(args) >= 2:
        delete_memory(args[1])
        return f"Memory deleted: {args[1]}"
    elif cmd == "clear":
        save_memory([])
        return "All memories cleared."
    elif cmd == "files":
        return json.dumps(list_stored_files(), indent=2)
    else:
        return "Usage: memory [list|add <key> <value>|delete <key>|clear|files]"


MODELS = {
    "gemini": ask_gemini,
    "chatgpt": ask_chatgpt,
    "perplexity": ask_perplexity,
    "perplexity_connectors": ask_perplexity_connectors,
    "lechat": ask_lechat,
    "chatai": ask_chatai,
    "arena": ask_arena,
    "arena_battle": ask_arena_battle,
    "arena_direct": ask_arena_direct,
    "battle3": ask_battle3,
    "random": ask_random,
    "pollinations": ask_pollinations,
    "flux": ask_flux,
    "pixelbin": ask_pixelbin,
}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Usage: python ai_proxy.py 'prompt' [model] [arena_model]\n"
                 "       python ai_proxy.py memory [list|add <key> <value>|delete <key>|clear|files]")
    if sys.argv[1].lower() == "memory":
        print(handle_memory_command(sys.argv[2:]), flush=True)
        sys.exit(0)
    prompt = sys.argv[1]
    model = (sys.argv[2] if len(sys.argv) > 2 else "gemini").lower()
    arena_model = sys.argv[3] if len(sys.argv) > 3 else "claude-sonnet-4-6-search"
    fn = MODELS.get(model, ask_gemini)
    if model == "arena_battle":
        print(ask_arena_battle(prompt), flush=True)
    elif model == "arena_direct":
        print(ask_arena_direct(prompt, arena_model), flush=True)
    else:
        print(fn(prompt), flush=True)
