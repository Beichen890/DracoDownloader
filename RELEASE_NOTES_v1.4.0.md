## 🚀 v1.4.0 - Resource Sniffer & Anti-Bot HTTP Client

### 🔍 Resource Sniffer
- Parse any web page URL into downloadable direct links (no more "page URL can't be downloaded")
- `ResourceSniffer` with pluggable `SniffRule` framework: sync `extract()` + async `extract_async()`
- Built-in generic rules: `_HTMLRule` (`<video>/<source>/<a>/<meta>/JSON-LD`), `_JSONRule`, `_RegexRule`
- Site rules run first; if hit, page fetch is skipped to bypass strong anti-bot pages (e.g. Bilibili 412)
- Direct-link recognition: 60+ file extensions (media / archive / installer / image / doc / torrent) + magnet
- Result ranked by confidence; `best` property returns the optimal resource (M3U8 > video > audio > ...)
- API: `DracoDownloader.sniff(url)` → `SniffResult.direct_urls` / `.best`

### 🌐 Site-Specific Rules
- **GitHub** (`_GitHubRule`): release tag / releases index / blob / repo root → direct links via GitHub API
  - Each direct link ships with 3 public proxy mirror candidates (gh-proxy.com / ghfast.top / gh.ddlc.top)
- **Bilibili** (`_BilibiliRule`): video page → DASH stream via view + playurl API
  - WBI signature algorithm (mixin_key permutation + w_rid MD5)
  - Multi-part support: `?p=1` / `?p=1-3` / `?p=1,3,5`
  - Picks highest-quality video + highest-quality audio; Referer injected via `extra.headers`

### 🛡️ Anti-Bot HTTP Client
- New `http_client.py`: shared persistent `aiohttp.ClientSession` for HTTP/M3U8/Sniffer
- UA rotation (desktop/mobile real-browser pool) + cookie jar reuse + real proxy activation
- Smart retry: 429/503 back off by `Retry-After`; network errors exponential backoff (1/2/4s)
- `AntiBotError` classification (403/412/429/401) for Agent programmatic handling
- Auto Referer injection for anti-leech sites
- `clean_headers` mode: strips browser-impersonation headers when calling REST APIs (avoids GitHub 403 / Bilibili 412)
- `set_cookies()` / `get_cookies()`: inject login cookies into the jar (domain-scoped to subdomains)

### 🔐 Bilibili Login (QR Code)
- New `auth.py`: `BilibiliAuth` — QR code login via passport API
  - `generate_qrcode()` → `poll_login()` (86039 waiting / 86090 scanned / 0 success) → cookie extraction
  - Cookie persistence to `~/.draco/auth/bilibili.json`; auto-loaded on startup
  - `check_login()` validates via nav API (username / uid / vip status)
- `DracoDownloader` API: `login_bilibili()` / `check_bilibili_login()` / `logout_bilibili()`
- Logged-in users get 1080P+ streams (vs 480P for guests)

### 🔧 Architecture
- `http_client.py`: new shared anti-bot HTTP client
- `sniffer.py`: new resource sniffer framework
- `sites.py`: new site-specific rules (GitHub / Bilibili)
- `auth.py`: new site login module (Bilibili QR)
- `core.py`: integrates sniffer + HttpClient + BilibiliAuth; auto-loads saved login on start
- `protocols/http.py`, `protocols/m3u8.py`: now use the shared HttpClient (UA/cookie/proxy/Referer)
- `errors.py`: new `AntiBotError` anti-bot error class

### 🧪 Tests
- `tests/test_sites.py`: site rules (URL match / API calls / WBI signature / multi-part / stream selection)
- `tests/test_sniffer_http.py`: sniffer framework + HTTP client
- `tests/test_direct_url_resolution.py`: direct-link resolution
- `tests/test_auth.py`: BilibiliAuth (QR generate/poll/login/check/persistence) + HttpClient cookie injection
- `.e2e/run_sites_e2e.py`: GitHub / Bilibili end-to-end + optional QR login (`python .e2e/run_sites_e2e.py login`)
