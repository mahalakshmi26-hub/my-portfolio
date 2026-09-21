"""
Robots.txt Generator — Flask dashboard
Built by Mahalakshmi Marimuthu · Digital Marketing Strategist & AI-Powered SEO Expert

Build a valid robots.txt file with a point-and-click interface: default access,
crawl-delay, sitemaps, per-crawler rules (search engines, AI crawlers, SEO tools),
allow/disallow paths, one-click presets (WordPress, Shopify, Magento, Block AI
crawlers, ...), a live preview, plain-English warnings, and a URL tester that
checks whether a path is allowed for a chosen bot (Google-style longest-match).

All generation and testing happens in the browser, so nothing typed into the
tool is ever sent to the server. Flask just serves the page.
"""

from flask import Flask, Response

app = Flask(__name__)


@app.route("/")
def index():
    return Response(PAGE_TEMPLATE, mimetype="text/html")


@app.route("/healthz")
def healthz():
    return {"status": "ok"}


PAGE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Robots.txt Generator | SEO Tools by Mahalakshmi</title>
<meta name="description" content="Free robots.txt generator. Set default access, crawl-delay, sitemaps and per-bot rules, block AI crawlers, use WordPress/Shopify presets, preview live, test URLs and download robots.txt.">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%237c6af7'/%3E%3Ctext x='50%25' y='50%25' dominant-baseline='central' text-anchor='middle' font-family='Georgia%2C serif' font-size='18' font-weight='900' fill='white'%3EM%3C/text%3E%3C/svg%3E">
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;600;700;800;900&family=Inter:wght@300;400;500;600&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0a0a0f; --surface: #12121a; --surface2: #1a1a26;
    --accent: #7c6af7; --accent2: #f7a26a; --accent3: #6af7c8;
    --danger: #f76a6a; --text: #e8e8f0; --muted: #8888a8;
    --border: rgba(124,106,247,0.18); --card: rgba(18,18,28,0.85);
  }
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text); font-size: 15px; line-height: 1.6; }
  a { color: inherit; }

  .layout { display: flex; min-height: 100vh; }

  /* SIDEBAR */
  .sidebar { width: 240px; flex-shrink: 0; background: var(--surface); border-right: 1px solid var(--border); padding: 1.6rem 1.2rem; position: sticky; top: 0; height: 100vh; display: flex; flex-direction: column; gap: 2rem; }
  .brand { display: flex; align-items: center; gap: 0.55rem; text-decoration: none; }
  .brand-mark { width: 30px; height: 30px; border-radius: 8px; background: var(--accent); display: flex; align-items: center; justify-content: center; font-family: 'Sora', sans-serif; font-weight: 900; font-size: 1rem; color: #fff; flex-shrink: 0; }
  .brand-text { font-family: 'Sora', sans-serif; font-weight: 700; font-size: 0.95rem; }
  .sidebar-section { display: flex; flex-direction: column; gap: 0.3rem; }
  .sidebar-label { font-family: 'DM Mono', monospace; font-size: 0.65rem; letter-spacing: 0.14em; text-transform: uppercase; color: var(--muted); margin-bottom: 0.5rem; }
  .sidebar-link { display: flex; align-items: center; gap: 0.6rem; padding: 0.55rem 0.7rem; border-radius: 8px; border: 1px solid transparent; font-size: 0.85rem; font-weight: 500; color: var(--muted); text-decoration: none; transition: all 0.15s; }
  .sidebar-link:hover { background: rgba(124,106,247,0.12); color: var(--text); }
  .sidebar-link.active { background: rgba(124,106,247,0.14); border: 1px solid rgba(124,106,247,0.4); color: var(--accent); font-weight: 700; }
  .sidebar-limits { margin-top: auto; background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 0.9rem; font-size: 0.72rem; color: var(--muted); line-height: 1.6; }
  .sidebar-limits strong { color: var(--text); }
  .sidebar-footer { padding-top: 1rem; border-top: 1px solid var(--border); }
  .sidebar-footer p { font-size: 0.7rem; color: var(--muted); line-height: 1.5; margin-bottom: 0.7rem; }
  .credit-name { color: var(--accent3); font-weight: 700; text-decoration: none; }
  .credit-name:hover { text-decoration: underline; }
  .sidebar-social { display: flex; gap: 0.5rem; }
  .sidebar-social a { width: 28px; height: 28px; border-radius: 6px; background: rgba(106,247,200,0.08); border: 1px solid rgba(106,247,200,0.35); display: flex; align-items: center; justify-content: center; color: var(--accent3); text-decoration: none; font-size: 0.68rem; font-family: 'DM Mono', monospace; transition: all 0.2s; }
  .sidebar-social a:hover { background: rgba(106,247,200,0.2); border-color: var(--accent3); }

  /* MAIN */
  .main { flex: 1; padding: 2.2rem 3rem 4rem; max-width: 1240px; min-width: 0; }
  .page-head { margin-bottom: 1.6rem; }
  .page-eyebrow { font-family: 'DM Mono', monospace; font-size: 0.68rem; letter-spacing: 0.16em; text-transform: uppercase; color: var(--accent3); margin-bottom: 0.5rem; }
  .page-title { font-family: 'Sora', sans-serif; font-size: 1.7rem; font-weight: 800; margin-bottom: 0.4rem; }
  .page-sub { color: var(--muted); font-size: 0.92rem; max-width: 680px; }

  .card { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 1.4rem; margin-bottom: 1.2rem; }
  .card h3 { font-family: 'Sora', sans-serif; font-size: 0.9rem; font-weight: 700; margin-bottom: 0.25rem; }
  .card .hint { font-size: 0.76rem; color: var(--muted); margin-bottom: 0.9rem; }

  .gen-grid { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr); gap: 1.2rem; align-items: start; }
  .preview-col { position: sticky; top: 1.2rem; }

  /* PRESETS */
  .preset-row { display: flex; gap: 0.5rem; flex-wrap: wrap; }
  .preset-btn { background: var(--surface2); border: 1px solid var(--border); color: var(--muted); padding: 0.45rem 0.9rem; border-radius: 100px; font-size: 0.76rem; font-family: 'DM Mono', monospace; cursor: pointer; transition: all 0.15s; }
  .preset-btn:hover { color: var(--text); border-color: var(--accent); }
  .preset-btn.active { background: var(--accent); border-color: var(--accent); color: #fff; }

  /* FORM */
  .field-row { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
  label.lbl { display: block; font-size: 0.74rem; font-family: 'DM Mono', monospace; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); margin-bottom: 0.4rem; }
  select, textarea, input[type=text], input[type=number] { width: 100%; background: var(--surface2); border: 1px solid var(--border); border-radius: 8px; color: var(--text); padding: 0.6rem 0.8rem; font-family: 'Inter', sans-serif; font-size: 0.85rem; }
  textarea { font-family: 'DM Mono', monospace; font-size: 0.8rem; min-height: 92px; resize: vertical; }
  select:focus, textarea:focus, input:focus { outline: none; border-color: var(--accent); }
  .field { margin-bottom: 1rem; }
  .field:last-child { margin-bottom: 0; }

  /* BOTS */
  .bot-group-title { font-family: 'DM Mono', monospace; font-size: 0.68rem; letter-spacing: 0.12em; text-transform: uppercase; color: var(--accent2); margin: 0.9rem 0 0.5rem; }
  .bot-group-title:first-of-type { margin-top: 0; }
  .bot-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0.5rem 1rem; }
  .bot-row { display: flex; align-items: center; justify-content: space-between; gap: 0.6rem; background: var(--surface2); border: 1px solid var(--border); border-radius: 8px; padding: 0.4rem 0.4rem 0.4rem 0.7rem; }
  .bot-name { font-size: 0.78rem; line-height: 1.25; min-width: 0; }
  .bot-name small { display: block; color: var(--muted); font-family: 'DM Mono', monospace; font-size: 0.64rem; overflow: hidden; text-overflow: ellipsis; }
  .bot-row select { width: 132px; flex-shrink: 0; padding: 0.35rem 0.4rem; font-size: 0.74rem; }
  .bot-row.is-allow { border-color: rgba(106,247,200,0.4); }
  .bot-row.is-refuse { border-color: rgba(247,106,106,0.45); }

  /* PREVIEW */
  .code-box { background: #08080d; border: 1px solid var(--border); border-radius: 10px; padding: 1rem; font-family: 'DM Mono', monospace; font-size: 0.78rem; line-height: 1.65; min-height: 220px; max-height: 420px; overflow: auto; white-space: pre; color: #cfd0ee; }
  .code-box .c-ua { color: var(--accent3); }
  .code-box .c-dis { color: var(--danger); }
  .code-box .c-allow { color: #8fe3a0; }
  .code-box .c-meta { color: var(--accent2); }
  .code-box .c-cmt { color: var(--muted); }
  .btn-row { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-top: 0.9rem; }
  .btn-primary { background: var(--accent); color: #fff; border: none; padding: 0.6rem 1.3rem; border-radius: 8px; font-family: 'Inter', sans-serif; font-weight: 600; font-size: 0.84rem; cursor: pointer; transition: background 0.2s, transform 0.2s; }
  .btn-primary:hover { background: #6a58e8; transform: translateY(-1px); }
  .btn-secondary { background: var(--surface2); border: 1px solid var(--border); color: var(--text); padding: 0.6rem 1.1rem; border-radius: 8px; font-size: 0.84rem; font-weight: 600; cursor: pointer; }
  .btn-secondary:hover { border-color: var(--accent); color: var(--accent); }
  .stat-strip { display: flex; gap: 0.6rem; flex-wrap: wrap; margin-bottom: 0.9rem; }
  .stat-pill { font-family: 'DM Mono', monospace; font-size: 0.7rem; padding: 0.25rem 0.7rem; border-radius: 100px; background: var(--surface2); border: 1px solid var(--border); color: var(--muted); }
  .stat-pill b { color: var(--text); }

  /* WARNINGS */
  .warn-list { list-style: none; display: flex; flex-direction: column; gap: 0.45rem; }
  .warn-item { font-size: 0.8rem; padding: 0.55rem 0.8rem; border-radius: 8px; border: 1px solid; }
  .warn-item.error { background: rgba(247,106,106,0.1); border-color: rgba(247,106,106,0.3); color: #ff9d9d; }
  .warn-item.warn { background: rgba(247,162,106,0.1); border-color: rgba(247,162,106,0.3); color: var(--accent2); }
  .warn-item.info { background: rgba(124,106,247,0.1); border-color: rgba(124,106,247,0.3); color: #b4a9ff; }
  .warn-item.ok { background: rgba(106,247,200,0.1); border-color: rgba(106,247,200,0.28); color: var(--accent3); }

  /* TESTER */
  .tester-row { display: grid; grid-template-columns: 1fr 170px auto; gap: 0.7rem; align-items: end; }
  .test-result { margin-top: 0.9rem; display: none; font-size: 0.85rem; }
  .badge { display: inline-block; font-family: 'DM Mono', monospace; font-size: 0.72rem; padding: 0.25rem 0.75rem; border-radius: 100px; font-weight: 600; margin-right: 0.5rem; }
  .badge.ok { background: rgba(106,247,200,0.12); color: var(--accent3); border: 1px solid rgba(106,247,200,0.3); }
  .badge.bad { background: rgba(247,106,106,0.12); color: var(--danger); border: 1px solid rgba(247,106,106,0.3); }

  /* LEARN */
  .learn-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; }
  .learn-grid .card { margin-bottom: 0; }
  .learn-grid p, .learn-grid li { font-size: 0.8rem; color: var(--muted); }
  .learn-grid ul { padding-left: 1.1rem; }
  .learn-grid code { font-family: 'DM Mono', monospace; color: var(--accent3); font-size: 0.75rem; }
  .toast { position: fixed; bottom: 1.5rem; right: 1.5rem; background: var(--accent3); color: #0a0a0f; padding: 0.6rem 1.1rem; border-radius: 8px; font-weight: 600; font-size: 0.82rem; opacity: 0; transform: translateY(10px); transition: all 0.25s; pointer-events: none; }
  .toast.show { opacity: 1; transform: none; }

  @media (max-width: 1000px) {
    .gen-grid { grid-template-columns: 1fr; }
    .preview-col { position: static; }
    .learn-grid { grid-template-columns: 1fr; }
  }
  @media (max-width: 900px) {
    .sidebar { display: none; }
    .main { padding: 1.6rem 1.2rem 3rem; }
    .bot-grid, .field-row, .tester-row { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<div class="layout">

  <aside class="sidebar">
    <a href="https://mahalakshmi26-hub.github.io/my-portfolio/tools/index.html" class="brand">
      <span class="brand-mark">M</span>
      <span class="brand-text">SEO Tools</span>
    </a>
    <div class="sidebar-section">
      <div class="sidebar-label">Tool</div>
      <a href="#" class="sidebar-link active">🤖 Robots.txt Generator</a>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-label">Navigate</div>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/tools/index.html" class="sidebar-link">🧰 All My Tools</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio" class="sidebar-link">👩‍💻 Portfolio</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/blog/index.html" class="sidebar-link">📝 Blog Posts</a>
    </div>
    <div class="sidebar-limits">
      Runs <strong>entirely in your browser</strong> — nothing you type is sent to a server.
    </div>
    <div class="sidebar-footer">
      <p>Built by <a href="https://mahalakshmi26-hub.github.io/my-portfolio" class="credit-name">Mahalakshmi Marimuthu</a><br>Digital Marketing Strategist &amp; AI-Powered SEO Expert</p>
      <div class="sidebar-social">
        <a href="https://linkedin.com/in/mahalakshmimarimuthu" target="_blank" title="LinkedIn">in</a>
        <a href="https://www.instagram.com/mahapravin26/" target="_blank" title="Instagram">IG</a>
        <a href="mailto:mahalakshmi.digitalpro@gmail.com" title="Email">✉</a>
      </div>
    </div>
  </aside>

  <main class="main">
    <div class="page-head">
      <div class="page-eyebrow">// seo tool</div>
      <div class="page-title">Robots.txt Generator</div>
      <div class="page-sub">Build a clean robots.txt in a few clicks — pick a preset, tune the rules for each crawler, watch the file update live, test any URL, then copy or download it.</div>
    </div>

    <div class="card">
      <h3>1. Start from a preset</h3>
      <div class="hint">Presets fill in the settings below. You can keep editing afterwards.</div>
      <div class="preset-row" id="presetRow"></div>
    </div>

    <div class="gen-grid">
      <div>
        <div class="card">
          <h3>2. Default rules</h3>
          <div class="hint">These apply to every crawler that has no rule of its own (<code>User-agent: *</code>).</div>
          <div class="field-row">
            <div class="field">
              <label class="lbl" for="defAccess">Default access</label>
              <select id="defAccess">
                <option value="allow">Allowed (crawl everything)</option>
                <option value="refuse">Refused (block everything)</option>
              </select>
            </div>
            <div class="field">
              <label class="lbl" for="delay">Crawl-delay</label>
              <select id="delay">
                <option value="0">No delay</option>
                <option value="5">5 seconds</option>
                <option value="10">10 seconds</option>
                <option value="20">20 seconds</option>
                <option value="60">60 seconds</option>
                <option value="120">120 seconds</option>
              </select>
            </div>
          </div>
          <div class="field">
            <label class="lbl" for="sitemaps">Sitemap URL(s) — optional, one per line</label>
            <textarea id="sitemaps" placeholder="https://www.example.com/sitemap.xml"></textarea>
          </div>
        </div>

        <div class="card">
          <h3>3. Block or allow specific paths</h3>
          <div class="hint">One path per line, starting with <code>/</code>. Wildcards <code>*</code> and <code>$</code> work (e.g. <code>/*?sort=</code>, <code>/*.pdf$</code>).</div>
          <div class="field">
            <label class="lbl" for="disallow">Disallow (restricted paths)</label>
            <textarea id="disallow" placeholder="/admin/&#10;/cart/&#10;/*?sessionid="></textarea>
          </div>
          <div class="field">
            <label class="lbl" for="allow">Allow (exceptions inside blocked paths)</label>
            <textarea id="allow" placeholder="/admin/public/"></textarea>
          </div>
        </div>

        <div class="card">
          <h3>4. Rules for individual crawlers</h3>
          <div class="hint">"Same as default" adds no extra rule. Choose Allowed or Refused to give a crawler its own rule.</div>
          <div id="botsWrap"></div>
        </div>
      </div>

      <div class="preview-col">
        <div class="card">
          <h3>Your robots.txt</h3>
          <div class="hint">Updates as you type. Upload the file to the root of your site (<code>/robots.txt</code>).</div>
          <div class="stat-strip" id="statStrip"></div>
          <div class="code-box" id="preview"></div>
          <div class="btn-row">
            <button class="btn-primary" id="copyBtn">Copy to clipboard</button>
            <button class="btn-secondary" id="downloadBtn">⬇ Download robots.txt</button>
            <button class="btn-secondary" id="resetBtn">Reset</button>
          </div>
        </div>

        <div class="card">
          <h3>Checks &amp; warnings</h3>
          <div class="hint">Plain-English notes about what your file will do.</div>
          <ul class="warn-list" id="warnList"></ul>
        </div>
      </div>
    </div>

    <div class="card">
      <h3>5. Test a URL against your rules</h3>
      <div class="hint">Uses Google's matching logic: the most specific (longest) matching rule wins, and Allow wins a tie.</div>
      <div class="tester-row">
        <div>
          <label class="lbl" for="testUrl">URL or path</label>
          <input type="text" id="testUrl" placeholder="https://www.example.com/admin/login or /admin/login">
        </div>
        <div>
          <label class="lbl" for="testBot">Crawler</label>
          <select id="testBot"></select>
        </div>
        <button class="btn-primary" id="testBtn">Test</button>
      </div>
      <div class="test-result" id="testResult"></div>
    </div>

    <div class="learn-grid">
      <div class="card">
        <h3>What is robots.txt?</h3>
        <p>A plain text file at the root of your site that tells crawlers which URLs they may request. It controls <em>crawling</em>, not indexing — a blocked URL can still appear in search if other sites link to it. Use a <code>noindex</code> tag to keep a page out of results, and never use robots.txt to hide private data.</p>
      </div>
      <div class="card">
        <h3>The four directives</h3>
        <ul>
          <li><code>User-agent</code> — which crawler the rules are for</li>
          <li><code>Disallow</code> — paths it must not crawl</li>
          <li><code>Allow</code> — exceptions to a Disallow</li>
          <li><code>Sitemap</code> — full URL of your XML sitemap</li>
        </ul>
      </div>
      <div class="card">
        <h3>Good to know</h3>
        <ul>
          <li>Googlebot ignores <code>Crawl-delay</code>; Bing and others honour it.</li>
          <li>A crawler with its own group ignores the <code>*</code> group, so this tool repeats your path rules inside groups for explicitly allowed bots.</li>
          <li>Don't block CSS or JS files Google needs to render pages.</li>
          <li>Bad bots simply ignore robots.txt.</li>
        </ul>
      </div>
    </div>
  </main>
</div>
<div class="toast" id="toast"></div>

<script>
// ==CORE== (pure logic — no DOM access; unit-tested with node)
const BOT_GROUPS = [
  { title: 'Search engines', bots: [
    ['Googlebot', 'Google'], ['Googlebot-Image', 'Google Images'], ['Googlebot-News', 'Google News'], ['Googlebot-Video', 'Google Video'],
    ['Bingbot', 'Bing'], ['Slurp', 'Yahoo'], ['DuckDuckBot', 'DuckDuckGo'], ['Baiduspider', 'Baidu'],
    ['YandexBot', 'Yandex'], ['Yeti', 'Naver'], ['Applebot', 'Apple (Siri / Spotlight)']
  ]},
  { title: 'AI crawlers', bots: [
    ['GPTBot', 'OpenAI training'], ['ChatGPT-User', 'ChatGPT browsing'], ['OAI-SearchBot', 'ChatGPT search'],
    ['ClaudeBot', 'Anthropic training'], ['Claude-User', 'Claude browsing'], ['PerplexityBot', 'Perplexity'],
    ['Google-Extended', 'Gemini / AI training'], ['Applebot-Extended', 'Apple AI training'], ['CCBot', 'Common Crawl'],
    ['Bytespider', 'ByteDance'], ['Amazonbot', 'Amazon'], ['Meta-ExternalAgent', 'Meta AI']
  ]},
  { title: 'SEO tool crawlers', bots: [
    ['AhrefsBot', 'Ahrefs'], ['SemrushBot', 'Semrush'], ['MJ12bot', 'Majestic'], ['DotBot', 'Moz / OpenSiteExplorer']
  ]}
];
const AI_BOTS = BOT_GROUPS[1].bots.map(b => b[0]);
const ALL_BOTS = BOT_GROUPS.flatMap(g => g.bots.map(b => b[0]));

const PRESETS = [
  { id: 'allow-all', label: 'Allow all', state: { def: 'allow' } },
  { id: 'block-all', label: 'Block all (staging)', state: { def: 'refuse' } },
  { id: 'block-ai', label: 'Block AI crawlers', state: { def: 'allow', bots: Object.fromEntries(AI_BOTS.map(b => [b, 'refuse'])) } },
  { id: 'allow-ai', label: 'Allow all + AI bots', state: { def: 'allow', bots: Object.fromEntries(AI_BOTS.map(b => [b, 'allow'])) } },
  { id: 'wordpress', label: 'WordPress', state: { def: 'allow', disallow: ['/wp-admin/', '/wp-login.php', '/*?s=', '/search/'], allow: ['/wp-admin/admin-ajax.php'] } },
  { id: 'shopify', label: 'Shopify', state: { def: 'allow', disallow: ['/admin', '/cart', '/checkout', '/checkouts/', '/orders', '/account', '/search', '/collections/*sort_by*', '/*?q='] } },
  { id: 'magento', label: 'Magento', state: { def: 'allow', disallow: ['/admin/', '/checkout/', '/customer/', '/catalogsearch/', '/*?dir=', '/*?limit=', '/*?order=', '/*?p='] } },
  { id: 'ecommerce', label: 'Generic e-commerce', state: { def: 'allow', disallow: ['/cart/', '/checkout/', '/account/', '/search/', '/*?sort=', '/*?filter=', '/*?sessionid='] } }
];

function blankState() {
  return { def: 'allow', delay: 0, sitemaps: [], disallow: [], allow: [], bots: {} };
}

function splitLines(s) {
  return String(s || '').split(/\r?\n/).map(x => x.trim()).filter(Boolean);
}

function normPath(p) {
  if (p.startsWith('/') || p.startsWith('*')) return p;
  return '/' + p;
}

function generate(st) {
  const warnings = [];
  const dis = [], alw = [];
  st.disallow.forEach(p => { const n = normPath(p); if (n !== p) warnings.push({ level: 'info', msg: `"${p}" did not start with "/", so it was written as "${n}".` }); dis.push(n); });
  st.allow.forEach(p => { const n = normPath(p); if (n !== p) warnings.push({ level: 'info', msg: `"${p}" did not start with "/", so it was written as "${n}".` }); alw.push(n); });

  const refused = ALL_BOTS.filter(b => st.bots[b] === 'refuse');
  const allowed = ALL_BOTS.filter(b => st.bots[b] === 'allow');
  const delayLine = st.delay > 0 ? ['Crawl-delay: ' + st.delay] : [];
  const pathRules = () => [...alw.map(p => 'Allow: ' + p), ...(dis.length ? dis.map(p => 'Disallow: ' + p) : ['Disallow:'])];

  const blocks = [];
  // default group
  const def = ['User-agent: *'];
  if (st.def === 'refuse') {
    alw.forEach(p => def.push('Allow: ' + p));
    def.push('Disallow: /');
    if (dis.length) warnings.push({ level: 'info', msg: 'Disallow paths are redundant while default access is "Refused" (everything is already blocked for the * group).' });
  } else {
    def.push(...pathRules());
  }
  def.push(...delayLine);
  blocks.push(def);

  if (refused.length) blocks.push([...refused.map(b => 'User-agent: ' + b), 'Disallow: /']);
  if (allowed.length) blocks.push([...allowed.map(b => 'User-agent: ' + b), ...pathRules(), ...delayLine]);

  const sm = [];
  st.sitemaps.forEach(u => {
    if (!/^https?:\/\/[^\s]+$/i.test(u)) warnings.push({ level: 'warn', msg: `Sitemap "${u}" should be a full URL starting with https:// (relative sitemap paths are not valid).` });
    sm.push('Sitemap: ' + u);
  });
  if (sm.length) blocks.push(sm);

  const text = blocks.map(b => b.join('\n')).join('\n\n') + '\n';

  // warnings
  const blockedGoogle = st.bots['Googlebot'] === 'refuse' || (st.def === 'refuse' && st.bots['Googlebot'] !== 'allow');
  if (st.def === 'refuse' && allowed.length === 0) warnings.unshift({ level: 'error', msg: 'This file blocks the whole site for every crawler, including Google. Only use it on staging or private sites.' });
  else if (blockedGoogle) warnings.unshift({ level: 'error', msg: 'Googlebot is blocked from crawling, so your pages will drop out of Google Search over time.' });
  if (dis.includes('/')) warnings.unshift({ level: 'error', msg: '"Disallow: /" blocks every URL for the default group.' });
  if (st.bots['Bingbot'] === 'refuse') warnings.push({ level: 'warn', msg: 'Bingbot is blocked — this also removes you from Bing, DuckDuckGo and Yahoo results that use its index.' });
  if (st.delay > 0) warnings.push({ level: 'info', msg: 'Crawl-delay is ignored by Googlebot. Bing, Yandex and some others honour it; a high delay can slow indexing on big sites.' });
  if (dis.some(p => /\.(css|js)\b|\/wp-content\/|\/wp-includes\/|\/assets\/|\/static\//i.test(p))) warnings.push({ level: 'warn', msg: 'You are blocking a path that may hold CSS/JS or theme files. Google needs these to render your pages properly.' });
  if (st.bots['Google-Extended'] === 'refuse') warnings.push({ level: 'info', msg: 'Blocking Google-Extended stops Gemini/AI training use only — it does not affect Google Search ranking.' });
  if (refused.some(b => AI_BOTS.includes(b))) warnings.push({ level: 'info', msg: 'AI crawler blocks only work for well-behaved bots that respect robots.txt.' });
  if (!st.sitemaps.length) warnings.push({ level: 'info', msg: 'No sitemap added. Adding your XML sitemap URL helps crawlers discover pages.' });
  if (!warnings.some(w => w.level === 'error' || w.level === 'warn')) warnings.push({ level: 'ok', msg: 'No problems found. Remember to upload the file to yourdomain.com/robots.txt.' });

  return { text, warnings, counts: { groups: blocks.filter(b => b[0].startsWith('User-agent')).length, refused: refused.length, allowed: allowed.length, disallow: dis.length, sitemaps: sm.length } };
}

function parseRobots(text) {
  const groups = [];
  let cur = null, lastUA = false;
  for (const raw of String(text).split(/\r?\n/)) {
    const line = raw.replace(/#.*$/, '').trim();
    if (!line) continue;
    const i = line.indexOf(':');
    if (i < 0) continue;
    const k = line.slice(0, i).trim().toLowerCase();
    const v = line.slice(i + 1).trim();
    if (k === 'user-agent') {
      if (!cur || !lastUA) { cur = { uas: [], rules: [] }; groups.push(cur); }
      cur.uas.push(v.toLowerCase());
      lastUA = true;
    } else if (k === 'allow' || k === 'disallow') {
      lastUA = false;
      if (cur) cur.rules.push({ type: k, path: v });
    } else {
      lastUA = false;
    }
  }
  return groups;
}

function pathToRegex(p) {
  let anchored = false;
  if (p.endsWith('$')) { anchored = true; p = p.slice(0, -1); }
  const re = p.replace(/[.+?^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*');
  return new RegExp('^' + re + (anchored ? '$' : ''));
}

function extractPath(input) {
  let s = String(input || '').trim();
  if (!s) return '';
  s = s.replace(/^[a-z][a-z0-9+.-]*:\/\/[^/?#]*/i, '');
  if (!s.startsWith('/')) s = '/' + s;
  return s.replace(/#.*$/, '');
}

function testUrl(text, ua, urlOrPath) {
  const groups = parseRobots(text);
  const path = extractPath(urlOrPath);
  const uaL = ua.toLowerCase();
  let matched = groups.filter(g => g.uas.includes(uaL));
  let usedUA = ua;
  if (!matched.length) { matched = groups.filter(g => g.uas.includes('*')); usedUA = '*'; }
  const rules = matched.flatMap(g => g.rules);
  let best = null;
  for (const r of rules) {
    if (r.path === '') continue;
    if (pathToRegex(r.path).test(path)) {
      const len = r.path.length;
      if (!best || len > best.path.length || (len === best.path.length && r.type === 'allow' && best.type !== 'allow')) best = r;
    }
  }
  return { allowed: !best || best.type === 'allow', rule: best, usedUA, path };
}
// ==/CORE==

// ---------- UI ----------
const $ = id => document.getElementById(id);
let activePreset = 'allow-all';

function buildUI() {
  const pr = $('presetRow');
  PRESETS.forEach(p => {
    const b = document.createElement('button');
    b.className = 'preset-btn'; b.dataset.id = p.id; b.textContent = p.label;
    b.onclick = () => applyPreset(p.id);
    pr.appendChild(b);
  });

  const wrap = $('botsWrap');
  BOT_GROUPS.forEach(g => {
    const t = document.createElement('div'); t.className = 'bot-group-title'; t.textContent = g.title; wrap.appendChild(t);
    const grid = document.createElement('div'); grid.className = 'bot-grid';
    g.bots.forEach(([ua, name]) => {
      const row = document.createElement('div'); row.className = 'bot-row'; row.id = 'row-' + ua;
      row.innerHTML = `<div class="bot-name">${name}<small>${ua}</small></div>
        <select data-bot="${ua}"><option value="default">Same as default</option><option value="allow">Allowed</option><option value="refuse">Refused</option></select>`;
      grid.appendChild(row);
    });
    wrap.appendChild(grid);
  });

  const tb = $('testBot');
  tb.innerHTML = '<option value="*">* (any other bot)</option>' + ALL_BOTS.map(b => `<option value="${b}"${b === 'Googlebot' ? ' selected' : ''}>${b}</option>`).join('');

  document.querySelectorAll('select, textarea').forEach(el => { if (el.id !== 'testBot') el.addEventListener('input', () => { setActivePreset(null); update(); }); });
  $('copyBtn').onclick = copyText;
  $('downloadBtn').onclick = downloadText;
  $('resetBtn').onclick = () => applyPreset('allow-all');
  $('testBtn').onclick = runTest;
  $('testUrl').addEventListener('keydown', e => { if (e.key === 'Enter') runTest(); });
}

function readState() {
  const st = blankState();
  st.def = $('defAccess').value;
  st.delay = parseInt($('delay').value, 10) || 0;
  st.sitemaps = splitLines($('sitemaps').value);
  st.disallow = splitLines($('disallow').value);
  st.allow = splitLines($('allow').value);
  document.querySelectorAll('select[data-bot]').forEach(s => { if (s.value !== 'default') st.bots[s.dataset.bot] = s.value; });
  return st;
}

function writeState(st) {
  $('defAccess').value = st.def;
  $('delay').value = String(st.delay);
  $('sitemaps').value = st.sitemaps.join('\n');
  $('disallow').value = st.disallow.join('\n');
  $('allow').value = st.allow.join('\n');
  document.querySelectorAll('select[data-bot]').forEach(s => { s.value = st.bots[s.dataset.bot] || 'default'; });
}

function setActivePreset(id) {
  activePreset = id;
  document.querySelectorAll('.preset-btn').forEach(b => b.classList.toggle('active', b.dataset.id === id));
}

function applyPreset(id) {
  const p = PRESETS.find(x => x.id === id);
  const keepSitemaps = splitLines($('sitemaps').value);
  const st = Object.assign(blankState(), p.state);
  st.sitemaps = keepSitemaps;
  writeState(st);
  setActivePreset(id);
  update();
}

function esc(s) { return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }

function highlight(text) {
  return text.split('\n').map(l => {
    const e = esc(l);
    if (/^user-agent:/i.test(l)) return `<span class="c-ua">${e}</span>`;
    if (/^disallow:/i.test(l)) return `<span class="c-dis">${e}</span>`;
    if (/^allow:/i.test(l)) return `<span class="c-allow">${e}</span>`;
    if (/^(sitemap|crawl-delay):/i.test(l)) return `<span class="c-meta">${e}</span>`;
    if (l.startsWith('#')) return `<span class="c-cmt">${e}</span>`;
    return e;
  }).join('\n');
}

let currentText = '';
function update() {
  const st = readState();
  const out = generate(st);
  currentText = out.text;
  $('preview').innerHTML = highlight(out.text);
  $('statStrip').innerHTML =
    `<span class="stat-pill"><b>${out.counts.groups}</b> rule groups</span>` +
    `<span class="stat-pill"><b>${out.counts.disallow}</b> blocked paths</span>` +
    `<span class="stat-pill"><b>${out.counts.refused}</b> bots refused</span>` +
    `<span class="stat-pill"><b>${out.counts.sitemaps}</b> sitemaps</span>`;
  $('warnList').innerHTML = out.warnings.map(w => `<li class="warn-item ${w.level}">${esc(w.msg)}</li>`).join('');
  document.querySelectorAll('select[data-bot]').forEach(s => {
    const row = $('row-' + s.dataset.bot);
    row.classList.toggle('is-allow', s.value === 'allow');
    row.classList.toggle('is-refuse', s.value === 'refuse');
  });
  if ($('testResult').style.display === 'block') runTest();
}

function toast(msg) {
  const t = $('toast'); t.textContent = msg; t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 1800);
}

function copyText() {
  const done = () => toast('Copied robots.txt to clipboard');
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(currentText).then(done, () => fallbackCopy(done));
  } else fallbackCopy(done);
}
function fallbackCopy(done) {
  const ta = document.createElement('textarea'); ta.value = currentText; document.body.appendChild(ta); ta.select();
  try { document.execCommand('copy'); done(); } catch (e) { toast('Copy failed — select the text manually'); }
  document.body.removeChild(ta);
}

function downloadText() {
  const blob = new Blob([currentText], { type: 'text/plain;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = 'robots.txt';
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(a.href), 500);
}

function runTest() {
  const url = $('testUrl').value.trim();
  const box = $('testResult');
  if (!url) { box.style.display = 'block'; box.innerHTML = '<span class="badge bad">Enter a URL or path first</span>'; return; }
  const ua = $('testBot').value;
  const r = testUrl(currentText, ua, url);
  const via = r.usedUA === '*' && ua !== '*' ? `no specific rule for ${esc(ua)}, so the default (*) group applies` : `group: ${esc(r.usedUA)}`;
  const rule = r.rule ? `matched <code>${esc(r.rule.type === 'allow' ? 'Allow' : 'Disallow')}: ${esc(r.rule.path)}</code>` : 'no matching rule, so crawling is allowed';
  box.style.display = 'block';
  box.innerHTML = `<span class="badge ${r.allowed ? 'ok' : 'bad'}">${r.allowed ? 'ALLOWED' : 'BLOCKED'}</span> <code>${esc(r.path)}</code> for <b>${esc(ua)}</b> — ${rule} (${via}).`;
}

buildUI();
applyPreset('allow-all');
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(debug=True, port=5052)
