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
        if result.error: print(f"  error: {result.error}")

async def main():
    await test_github()
    await test_github_blob()
    await test_bilibili()
    banner("实测完成")

asyncio.run(main())
