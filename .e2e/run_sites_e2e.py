"""站点规则端到端实测：GitHub release + Bilibili 视频"""
import asyncio, sys, time
sys.path.insert(0, '/workspace/.e2e')
from DracoDownloader import DracoDownloader

def banner(t): print(f"\n{'='*70}\n  {t}\n{'='*70}")

async def test_github():
    banner("GitHub release 页探嗅（API 获取 assets + 加速站候选）")
    async with DracoDownloader() as d:
        url = "https://github.com/BurntSushi/ripgrep/releases/tag/14.1.1"
        print(f"探嗅: {url}")
        t0 = time.time()
        result = await d.sniff(url)
        print(f"耗时: {time.time()-t0:.2f}s, 候选数: {len(result.direct_urls)}")
        for r in result.direct_urls[:12]:
            print(f"  [{r.type.value:8s}] conf={r.confidence:.2f} src={r.source:25s} label={r.label[:30]:30s} {r.url[:80]}")
        if result.error: print(f"  error: {result.error}")

async def test_github_blob():
    banner("GitHub blob 页探嗅（转 raw + 加速站候选）")
    async with DracoDownloader() as d:
        url = "https://github.com/BurntSushi/ripgrep/blob/master/README.md"
        print(f"探嗅: {url}")
        result = await d.sniff(url)
        print(f"候选数: {len(result.direct_urls)}")
        for r in result.direct_urls[:6]:
            print(f"  [{r.type.value:8s}] src={r.source:25s} {r.url[:90]}")

async def test_bilibili():
    banner("Bilibili 视频页探嗅（API 获取 DASH 流，绕过 412）")
    async with DracoDownloader() as d:
        url = "https://www.bilibili.com/video/BV1GJ411x7h7"
        print(f"探嗅: {url}")
        t0 = time.time()
        result = await d.sniff(url)
        print(f"耗时: {time.time()-t0:.2f}s, 候选数: {len(result.direct_urls)}")
        for r in result.direct_urls[:8]:
            print(f"  [{r.type.value:8s}] conf={r.confidence:.2f} src={r.source:18s} label={r.label[:40]}")
            print(f"    url: {r.url[:100]}")
            if r.extra.get('audio_url'):
                print(f"    audio: {r.extra['audio_url'][:90]}")
            if r.extra.get('headers'):
                print(f"    headers: {r.extra['headers']}")
            if r.extra.get('video_quality') is not None:
                print(f"    video_quality: {r.extra['video_quality']}")
        if result.error: print(f"  error: {result.error}")

async def test_bilibili_login():
    """Bilibili 扫码登录 + 登录后探嗅验证高画质（需人工扫码）"""
    banner("Bilibili 扫码登录（需手机 B 站 App 扫码确认）")
    # 用本地文件持久化 cookie，登录后下次启动自动加载
    async with DracoDownloader(bilibili_storage_path='/tmp/draco_bili_login.json') as d:
        # 1. 检查现有登录态
        status = await d.check_bilibili_login()
        print(f"登录前状态: {status['is_logged_in']} | {status['message']}")
        if status['is_logged_in']:
            print(f"  已登录用户: {status['username']} (uid={status['uid']}, vip={status['vip_status']})")
            print("  跳过扫码，直接探嗅验证画质")
        else:
            print("\n请用手机 B 站 App 扫描以下二维码 URL（复制到二维码生成器或浏览器打开）：")
            qr_url_box = []

            async def on_qrcode(url):
                qr_url_box.append(url)
                print(f"\n  二维码 URL: {url}\n")
                print("  状态: 等待扫码...")

            async def on_status(code, msg):
                print(f"  状态更新: code={code} {msg}")

            result = await d.login_bilibili(
                on_qrcode=on_qrcode, on_status=on_status, timeout=180,
            )
            if not result['success']:
                print(f"  登录失败: {result['message']}")
                return
            print(f"  登录成功! 用户: {result.get('username', '')}")
            print(f"  cookie 已持久化到 /tmp/draco_bili_login.json")

        # 2. 登录后探嗅，验证画质提升（登录后应能拿 1080P+）
        print("\n登录后探嗅 B 站视频（验证画质提升）:")
        url = "https://www.bilibili.com/video/BV1GJ411x7h7"
        result = await d.sniff(url)
        print(f"候选数: {len(result.direct_urls)}")
        for r in result.direct_urls[:4]:
            q = r.extra.get('video_quality')
            print(f"  label={r.label} video_quality={q}")
        if result.error:
            print(f"  error: {result.error}")

async def main():
    await test_github()
    await test_github_blob()
    await test_bilibili()
    # 登录实测需人工扫码，单独运行：python .e2e/run_sites_e2e.py login
    if len(sys.argv) > 1 and sys.argv[1] == 'login':
        await test_bilibili_login()
    else:
        print("\n(跳过扫码登录实测，运行 'python .e2e/run_sites_e2e.py login' 启用)")
    banner("实测完成")

asyncio.run(main())
