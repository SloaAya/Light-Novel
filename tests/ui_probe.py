# -*- coding: utf-8 -*-
"""UI 探针：用真实 Edge（Playwright，复用系统浏览器，不额外下载）验证 OPDS 页面的
「未读圆圈」显隐，并把关键状态拼成一张对比图。

为什么要它：源码断言（smoke_test 的 R4b）只能证明 CSS 文本写对了，
证明不了浏览器**真的**按它渲染 —— 这次就是靠它才看清
「POST→303 整页重载后浏览器不会立刻重算 :hover」这个真实行为。

跑法：
    .venv/Scripts/python.exe tests/ui_probe.py            # 默认 8099 端口

依赖：playwright（python 包）+ 系统 Edge。没装 playwright 时脚本会提示并跳过（退出码 0）。
产物：.autosync/probe/*.png（含 compare.png 对比图）
写入：临时 OPDS 会真的 POST 一次标记再撤销；finished.json 先快照、finally 里强制还原。
"""
import os
import shutil
import subprocess
import sys
import time
from urllib.parse import quote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PORT = int(os.environ.get("PROBE_PORT", "8099"))
OUT = os.path.join(ROOT, ".autosync", "probe")
FIN = os.path.join(ROOT, ".autosync", "finished.json")
CAT = "已完结"
URL_CAT = f"http://127.0.0.1:{PORT}/opds/catalog/{quote(CAT)}"
URL_READ = f"http://127.0.0.1:{PORT}/opds/read"

fails = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {got!r}" + ("" if ok else f"   期望 {want!r}"))
    if not ok:
        fails.append(name)


def px(loc):
    """元素计算样式里的 (opacity, pointer-events)。"""
    return loc.evaluate("el => { const s = getComputedStyle(el); return [s.opacity, s.pointerEvents]; }")


def wait_up(pg, url, tries=40):
    for _ in range(tries):
        try:
            r = pg.goto(url, timeout=2000)
            if r and r.status == 200:
                return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError(f"临时 OPDS 没起来：{url}")


def run_probe():
    from playwright.sync_api import sync_playwright

    # 4x 放大：26px 的圆圈在整图里太小，必须放大才看得清
    px_view = {"width": 880, "height": 620}
    with sync_playwright() as p:
        br = p.chromium.launch(channel="msedge", headless=True)
        pg = br.new_page(viewport=px_view, device_scale_factor=4)
        wait_up(pg, URL_CAT)
        pg.wait_for_timeout(500)

        # 关掉懒加载造成的抖动
        pg.evaluate("window.scrollTo(0,0)")
        pg.wait_for_timeout(400)

        cards = pg.locator(".card")
        n_mk = pg.locator(".card .mk").count()
        print(f"卡片数 {cards.count()}，标记按钮数 {n_mk}")
        check("管理员视角出现标记按钮", n_mk > 0, True)
        if not n_mk:
            raise RuntimeError("没有标记按钮，无法继续")

        card, mk = cards.first, cards.first.locator(".mk")
        back = card.evaluate("el => el.querySelector('.cardlink').getAttribute('href')")

        def zoom(name):
            """把按钮左上角附近 100x100 区域放大成图。"""
            b = mk.bounding_box()
            pg.screenshot(path=os.path.join(OUT, name),
                          clip={"x": b["x"] - 18, "y": b["y"] - 18, "width": 104, "height": 104})

        # A. 静息态
        pg.mouse.move(4, 4)
        pg.wait_for_timeout(400)
        check("A 静息 opacity", px(mk)[0], "0")
        check("A 静息 pointer-events", px(mk)[1], "none")
        zoom("z1_rest.png")

        # B. 悬停整张卡
        card.hover()
        pg.wait_for_timeout(400)
        check("B 悬停卡 opacity", px(mk)[0], "1")
        check("B 悬停卡 pointer-events", px(mk)[1], "auto")
        zoom("z2_hover.png")

        # C. 键盘聚焦（这就是刻意不用 visibility:hidden 的原因）
        pg.mouse.move(4, 4)
        pg.wait_for_timeout(300)
        check("C 键盘可聚焦",
              mk.evaluate("el => { el.focus(); return document.activeElement === el; }"), True)
        pg.wait_for_timeout(400)
        check("C 聚焦即显形 opacity", px(mk)[0], "1")
        zoom("z3_focus.png")

        # D. 真实点击 → 标记已读完（整页重载），再悬停看 .on 的着色态
        pg.mouse.move(4, 4)
        card.hover()
        mk.click()
        pg.wait_for_timeout(900)
        check("D 点击后 class（重载后）", mk.get_attribute("class"), "mk on")
        check("D 卡片副标题出现「已读完」",
              card.locator(".s .fin").inner_text().strip(), "已读完")
        # 重载瞬间浏览器不会自己重算 :hover（Chrome/Edge 已知行为）——见下方 C1 诊断
        check("D 重载瞬间 opacity（浏览器尚未重算 hover）", px(mk)[0], "0")
        b = mk.bounding_box()
        pg.mouse.move(b["x"] + b["width"] / 2 + 2, b["y"] + b["height"] / 2 + 2)
        pg.wait_for_timeout(400)
        check("D' 指针微动（仍在按钮上）→ 恢复显示", px(mk)[0], "1")
        zoom("z4_marked.png")

        # E. 撤掉悬停 + 强制失焦 → 必须回到隐藏，不留残影
        pg.mouse.move(880, 610)
        pg.evaluate("document.activeElement && document.activeElement.blur()")
        pg.wait_for_timeout(500)
        check("E 失焦+移开 opacity 回到", px(mk)[0], "0")
        check("E 失焦+移开 pointer-events 回到", px(mk)[1], "none")

        # F. 撤销标记，把书库还原
        card.hover()
        mk.click()
        pg.wait_for_timeout(900)
        check("F 取消后 class", mk.get_attribute("class"), "mk")
        check("F 取消后副标题「已读完」消失", card.locator(".s .fin").count(), 0)

        # G. 整页网格图（1x，看整体有没有圆圈残留）
        pg2 = br.new_page(viewport=px_view, device_scale_factor=1)
        wait_up(pg2, URL_CAT)
        pg2.wait_for_timeout(600)
        pg2.mouse.move(4, 4)
        pg2.wait_for_timeout(400)
        clip = {"x": 0, "y": 150, "width": 880, "height": 330}
        pg2.screenshot(path=os.path.join(OUT, "g1_grid_rest.png"), clip=clip)
        pg2.locator(".card").first.hover()
        pg2.wait_for_timeout(400)
        pg2.screenshot(path=os.path.join(OUT, "g2_grid_hover.png"), clip=clip)

        # H. 已读完清单页
        pg2.goto(URL_READ, timeout=6000)
        pg2.wait_for_timeout(400)
        n_fin = pg2.locator(".card").count()
        body = pg2.locator("body").inner_text()
        print(f"      · 已读完清单当前 {n_fin} 部（本地真实数据）")
        check("H 清单页提到「移上封面」", "移上封面" in body, True)
        if n_fin == 0:
            check("H 空态提到触屏例外", "触屏" in body, True)
        else:
            print("      · 清单非空 → 空态文案断言跳过（空态文案由 smoke_test 覆盖）")
        pg2.screenshot(path=os.path.join(OUT, "g3_read.png"),
                       clip={"x": 0, "y": 60, "width": 880, "height": 300})
        br.close()


def compose():
    """把四张放大图拼成一张对比页，截图存 compare.png（纯 HTML + Edge，无需 Pillow）。"""
    from playwright.sync_api import sync_playwright

    items = [("静息（鼠标不在卡上）", "z1_rest.png", "圆圈不可见 —— 不再常驻"),
             ("悬停整张卡", "z2_hover.png", "圆圈淡入（opacity 0 → 1）"),
             ("键盘 Tab 聚焦", "z3_focus.png", "聚焦即显形 —— 所以没用 visibility:hidden"),
             ("已标记（悬停时）", "z4_marked.png", "○ 变实心 ✓，副标题同步写「已读完」")]
    cells = "".join(
        f'<figure><img src="{f}"><figcaption><b>{t}</b><span>{d}</span></figcaption></figure>'
        for t, f, d in items)
    grid = "".join(
        f'<figure class="wide"><img src="{f}"><figcaption><b>{t}</b></figcaption></figure>'
        for t, f in [("整页网格 · 静息：全站一个圆圈都没有", "g1_grid_rest.png"),
                     ("整页网格 · 悬停第一本：只有它冒出圆圈", "g2_grid_hover.png")])
    html = f"""<!doctype html><meta charset="utf-8">
<style>
 body{{margin:0;padding:26px;background:#f5f2ed;color:#22282f;
      font-family:"Microsoft YaHei UI","Segoe UI",system-ui,sans-serif}}
 h1{{font-size:19px;margin:0 0 4px}} .sub{{color:#6b7280;font-size:13px;margin-bottom:20px}}
 .row{{display:flex;gap:18px;flex-wrap:wrap}}
 figure{{margin:0;background:#fff;border:1px solid #e3ded6;border-radius:12px;padding:12px}}
 .row figure{{width:236px}} .row img{{width:212px;height:212px;display:block;border-radius:8px}}
 .wide{{width:880px;margin-top:18px}} .wide img{{width:856px;display:block;border-radius:8px}}
 figcaption{{font-size:12.5px;margin-top:9px;line-height:1.5}}
 figcaption b{{display:block;font-size:13px}} figcaption span{{color:#6b7280}}
 .wide b{{font-size:13px}}
</style>
<h1>「未读圆圈」显隐 —— 真实浏览器渲染结果</h1>
<div class="sub">同一本书的封面左上角，四种状态；图片为 4 倍放大。</div>
<div class="row">{cells}</div>
{grid}
"""
    p_html = os.path.join(OUT, "_compare.html")
    with open(p_html, "w", encoding="utf-8") as f:
        f.write(html)
    with sync_playwright() as p:
        br = p.chromium.launch(channel="msedge", headless=True)
        pg = br.new_page(viewport={"width": 980, "height": 1200}, device_scale_factor=1)
        pg.goto("file:///" + p_html.replace("\\", "/"), timeout=8000)
        pg.wait_for_timeout(700)
        pg.screenshot(path=os.path.join(OUT, "compare.png"), full_page=True)
        br.close()
    os.remove(p_html)


def main():
    os.makedirs(OUT, exist_ok=True)
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("未安装 playwright，跳过 UI 探针（pip install playwright 即可，复用系统 Edge）。")
        return 0

    had_fin = os.path.exists(FIN)
    fin_bak = FIN + ".probe-bak"
    if had_fin:
        shutil.copy2(FIN, fin_bak)

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(
        [sys.executable, "-m", "lightnovel", "opds", "--port", str(PORT),
         "--bind", "127.0.0.1", "--no-qr"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        run_probe()
        compose()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        if had_fin:                       # 强制还原，别把探针的标记留在真实清单里
            shutil.copy2(fin_bak, FIN)
            os.remove(fin_bak)
        elif os.path.exists(FIN):
            os.remove(FIN)

    print("-" * 60)
    print(f"  失败 {len(fails)} 项" + (f"：{fails}" if fails else "（全部通过）"))
    print(f"  产物：{os.path.join(OUT, 'compare.png')}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
