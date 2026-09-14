# -*- coding: utf-8 -*-
"""UI 探针：用真实 Edge（Playwright，复用系统浏览器，不额外下载）核对 OPDS 页面的
实际渲染，并把关键状态拼成对比图。

为什么要它：源码断言（smoke_test 里的正则/结构断言）只能证明「HTML/CSS 文本写对了」，
证明不了浏览器**真的**按它渲染、人眼**真的**看得见 —— 这次就是靠它才看清两点：
  1) 26px 的小圆圈在整页截图里肉眼几乎看不见（得放大 4 倍才看得清）；
  2) 点上标记按钮后**整页重载**：滚动位置丢失、画面闪一下，而且重载瞬间浏览器不会
     重算 :hover，指针明明停在按钮上、圆圈却短暂消失。

第 2 点后来改成了原地提交（fetch + 就改 DOM）。所以这里除了「看得见」，还专门验证
「点完没重载」：在 window 上埋一个标记，重载会把它抹掉 —— 这比看截图可靠得多。

跑法：
    .venv/Scripts/python.exe tests/ui_probe.py            # 默认 8099 端口

依赖：playwright（python 包）+ 系统 Edge。没装 playwright 时脚本会提示并跳过（退出码 0）。
产物：.autosync/probe/*.png（compare.png = 未读圆圈；compare_updates.png = 新增卷提示）
写入：① 临时 OPDS 会真的 POST 几次「已读完」标记再撤销；
      ② 新增卷提示那一段会往 updates.json **塞一条人造待读提示**（指向真实存在的卷号，
         免得真往书库里加文件被监控提交推送出去），截完图立刻还原。
      finished.json / updates.json 都先快照、finally 里强制还原。
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
UPD_FILE = os.path.join(ROOT, ".autosync", "updates.json")
CAT = "已完结"
URL_CAT = f"http://127.0.0.1:{PORT}/opds/catalog/{quote(CAT)}"
URL_READ = f"http://127.0.0.1:{PORT}/opds/read"

fails = []
STATS = {}                      # 拼对比图时要用的真实数字（别在图里写「N 张」这种占位）


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
        sub0 = card.locator(".s").inner_text().strip()
        print(f"      · 目标卡：{card.locator('.t').inner_text()} / 副标题 {sub0!r}")

        def zoom(name):
            """把按钮左上角附近 100x100 区域放大成图。"""
            b = mk.bounding_box()
            pg.screenshot(path=os.path.join(OUT, name),
                          clip={"x": b["x"] - 18, "y": b["y"] - 18, "width": 104, "height": 104})

        def no_reload(before, label):
            """页面有没有被重载：window 上的标记在重载后会消失（新 document）。

            比截图像素比对可靠：内容区高度会变、懒加载图片会闪，逐像素比会全是假阳性。
            """
            alive = pg.evaluate("window.__probeMark === 1 && window.__probeMemo === "
                                + repr(before))
            check(label, alive, True)

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

        # D. 真实点击 → 原地标记（不刷新页面）。埋标记：重载会把它抹掉。
        pg.evaluate("window.scrollTo(0, 90)")
        pg.wait_for_timeout(300)
        card.hover()
        y0 = pg.evaluate("Math.round(window.scrollY)")
        pg.evaluate("window.__probeMark = 1; window.__probeMemo = 'd';")
        pg.wait_for_timeout(200)
        mk.click()
        pg.wait_for_timeout(700)
        no_reload("d", "D 点击后页面**没有重载**（window 标记还在）")
        check("D 滚动位置没被重置（重载会跳回顶部）",
              pg.evaluate("Math.round(window.scrollY)"), y0)
        check("D 点击后 class（原地改，无需等重载）", mk.get_attribute("class"), "mk on")
        check("D 副标题就地加上「已读完」",
              card.locator(".s").inner_text().strip(), sub0 + " · 已读完")
        # 没有重载 → 浏览器不用重算 :hover，圆圈不会「点完就消失」（旧行为的那个怪象）
        check("D 点击后圆圈仍可见（旧版这里会瞬间消失）", px(mk)[0], "1")
        zoom("z4_marked.png")

        # E. 撤掉悬停 + 强制失焦 → 必须回到隐藏，不留残影
        pg.mouse.move(880, 610)
        pg.evaluate("document.activeElement && document.activeElement.blur()")
        pg.wait_for_timeout(500)
        check("E 失焦+移开 opacity 回到", px(mk)[0], "0")
        check("E 失焦+移开 pointer-events 回到", px(mk)[1], "none")

        # F. 撤销标记（同样不重载），把书库还原；副标题要回到原样、不留孤零零的「·」
        pg.evaluate("window.__probeMemo = 'f';")
        card.hover()
        mk.click()
        pg.wait_for_timeout(700)
        no_reload("f", "F 取消后页面**没有重载**")
        check("F 取消后 class", mk.get_attribute("class"), "mk")
        check("F 取消后副标题「已读完」消失", card.locator(".s .fin").count(), 0)
        check("F 副标题回到原样（分隔符没有残留）",
              card.locator(".s").inner_text().strip(), sub0)

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
        pg2.screenshot(path=os.path.join(OUT, "g5_read.png"),
                       clip={"x": 0, "y": 60, "width": 880, "height": 300})

        # I. 「已读完」清单页：取消标记 → 卡片就地消失 + 计数减 1，且仍然不重载
        pg2.goto(URL_CAT, timeout=6000)
        pg2.wait_for_timeout(500)
        first = pg2.locator(".card").first
        book = first.locator(".t").inner_text()
        first.hover()
        pg2.wait_for_timeout(250)
        first.locator(".mk").click()                 # 先标一部，保证清单非空
        pg2.wait_for_timeout(600)
        pg2.goto(URL_READ, timeout=6000)
        pg2.wait_for_timeout(500)
        n0 = pg2.locator(".card").count()
        total0 = int(pg2.locator(".grid").get_attribute("data-total"))
        print(f"      · 清单：本页 {n0} 张 / 合计 {total0} 部（刚标上的是「{book}」）")
        STATS.update(read_n0=n0, read_total0=total0, read_book=book)
        pg2.mouse.move(4, 4)                         # 静息态截图：不把悬停圆圈拍进去
        pg2.wait_for_timeout(300)
        pg2.screenshot(path=os.path.join(OUT, "g3_read.png"),
                       clip={"x": 0, "y": 60, "width": 880, "height": 300})
        pg2.evaluate("window.__probeMark = 1; window.__probeMemo = 'i';")
        tgt = pg2.locator(".card").first
        tgt.hover()
        pg2.wait_for_timeout(250)
        tgt.locator(".mk").click()
        pg2.wait_for_timeout(800)
        check("I 取消后卡片就地消失（这份清单里不该留着「未读完」的书）",
              pg2.locator(".card").count(), n0 - 1)
        check("I 计数同步减 1（用的是服务端下发的总数，不是数当前页的卡）",
              pg2.locator("#readcnt").inner_text().strip(), f"{total0 - 1} 部作品")
        check("I 仍然没有重载",
              pg2.evaluate("window.__probeMark === 1 && window.__probeMemo === 'i'"), True)
        pg2.mouse.move(4, 4)
        pg2.wait_for_timeout(300)
        pg2.screenshot(path=os.path.join(OUT, "g4_read_after.png"),
                       clip={"x": 0, "y": 60, "width": 880, "height": 300})
        br.close()


def compose():
    """把四张放大图拼成一张对比页，截图存 compare.png（纯 HTML + Edge，无需 Pillow）。"""
    from playwright.sync_api import sync_playwright

    items = [("静息（鼠标不在卡上）", "z1_rest.png", "圆圈不可见 —— 不再常驻"),
             ("悬停整张卡", "z2_hover.png", "圆圈淡入（opacity 0 → 1）"),
             ("键盘 Tab 聚焦", "z3_focus.png", "聚焦即显形 —— 所以没用 visibility:hidden"),
             ("已标记（悬停时）", "z4_marked.png",
              "○ 变实心 ✓，副标题同步写「已读完」；原地生效，页面没有重载")]
    cells = "".join(
        f'<figure><img src="{f}"><figcaption><b>{t}</b><span>{d}</span></figcaption></figure>'
        for t, f, d in items)
    grid = "".join(
        f'<figure class="wide"><img src="{f}"><figcaption><b>{t}</b></figcaption></figure>'
        for t, f in [("整页网格 · 静息：全站一个圆圈都没有", "g1_grid_rest.png"),
                     ("整页网格 · 悬停第一本：只有它冒出圆圈", "g2_grid_hover.png"),
                     (f"「已读完」清单 · 取消前：本页 {STATS.get('read_n0', '?')} 张卡、"
                      f"计数 {STATS.get('read_total0', '?')} 部", "g3_read.png"),
                     (f"「已读完」清单 · 取消后：那一张就地消失、计数减到 "
                      f"{STATS.get('read_total0', '?') - 1 if STATS.get('read_total0') else '?'} 部，"
                      f"页面没有重载（滚动位置也没动）", "g4_read_after.png")])
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
<h1>「未读圆圈」显隐 + 原地标记 —— 真实浏览器渲染结果</h1>
<div class="sub">同一本书的封面左上角，四种状态（4 倍放大）。点标记**不再刷新页面**：
探针在 window 上埋了标记，点击后它仍然在、滚动位置也没被重置 —— 这几条是断言，不只是截图。</div>
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


# ------------------------- 新增卷提示（人造状态） -------------------------
def _pick_book_with_subdirs():
    """挑一部**真实存在**、子目录最多的作品当演示对象（「主线正传/安可系列/外传…」那种结构）。

    子目录多才看得出「点名是哪一册」的价值：新卷常常落在默认折叠的靠后分组里。
    """
    from lightnovel.opds import library as lib
    data = lib.get_library()
    best = None
    for cat, books in data.items():
        for book, vols in books.items():
            withsub = [v for v in vols if "/" in v["rel"][len(f"{cat}/{book}/"):]]
            n_sub = len({v["rel"][len(f"{cat}/{book}/"):].rsplit("/", 1)[0] for v in withsub})
            if withsub and (best is None or n_sub > best[4]):
                best = (cat, book, vols, withsub, n_sub)
    if best:
        return best[:4]
    for cat, books in data.items():                  # 一个子目录都没有时就退到第一部作品
        for book, vols in books.items():
            return cat, book, vols, vols
    return None


def probe_updates():
    """给「新增卷提示」截图：置顶后的列表页 + 详情页点名哪一卷。

    状态是人造的：`updates.json` 里塞一条指向**真实存在的卷**的待读提示。
    不真往书库加文件 —— 那个目录被监控盯着，加一个文件就会被提交并推上 GitHub。
    """
    import json

    from playwright.sync_api import sync_playwright

    from lightnovel.opds import updates as upd

    had = os.path.exists(UPD_FILE)
    bak = UPD_FILE + ".probe-bak"
    if had:
        shutil.copy2(UPD_FILE, bak)
    try:
        upd.observe()                                   # 先写一份与真实书库一致的基线
        picked = _pick_book_with_subdirs()
        if not picked:
            print("      · 书库为空，跳过新增卷提示截图")
            return
        cat, book, vols, withsub = picked
        key = f"{cat}/{book}"
        new_rel = withsub[-1]["rel"]                    # 拿真实存在的一卷「冒充」新卷
        with open(UPD_FILE, encoding="utf-8") as f:
            state = json.load(f)
        state["pending"] = {key: {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "vols": [new_rel]}}
        with open(UPD_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, separators=(",", ":"))
        print(f"      · 演示对象：{key} → {new_rel}")

        with sync_playwright() as p:
            br = p.chromium.launch(channel="msedge", headless=True)
            pg = br.new_page(viewport={"width": 980, "height": 1400}, device_scale_factor=1)
            for _ in range(40):
                try:
                    if pg.goto(URL_CAT, timeout=2000).status == 200:
                        break
                except Exception:
                    time.sleep(0.3)
            pg.wait_for_timeout(700)
            pg.screenshot(path=os.path.join(OUT, "u1_list.png"),
                          clip={"x": 0, "y": 100, "width": 980, "height": 480})
            card = pg.locator(".card").first
            badge = (card.locator(".upd").inner_text() if card.locator(".upd").count() else "无")
            print(f"      · 列表首张卡：{card.locator('.t').inner_text()} / 角标 {badge}")

            pg.goto(f"http://127.0.0.1:{PORT}/opds/book/" + quote(key, safe=""), timeout=8000)
            pg.wait_for_timeout(800)
            body = pg.locator("body").inner_text()
            left = upd.load_pending()
            print(f"      · 详情页含「本次新增」：{'本次新增' in body}；"
                  f"访问后提示已被收走：{not left}")
            # 分两张截：① 顶部「本次新增」区块 ② 那个有新卷的分组（它可能排在很后面，挤一张图看不清）
            box = pg.locator(".updbox").bounding_box()
            print(f"      · 新增区块位置：y={box['y']:.0f} h={box['height']:.0f}")
            pg.screenshot(path=os.path.join(OUT, "u2_detail.png"), full_page=True,
                          clip={"x": 0, "y": max(0, box["y"] - 26),
                                "width": 980, "height": box["height"] + 52})
            hot = pg.locator(".group").filter(has=pg.locator(".gnew")).first
            if hot.count():
                hb = hot.bounding_box()
                print(f"      · 有新卷的分组位置：y={hb['y']:.0f} h={hb['height']:.0f}")
                pg.screenshot(path=os.path.join(OUT, "u3_group.png"), full_page=True,
                              clip={"x": 0, "y": max(0, hb["y"] - 14),
                                    "width": 980, "height": hb["height"] + 18})
                print(f"      · 有新卷的分组：{hot.locator('.gnm').inner_text()} "
                      f"{hot.locator('.gnew').inner_text()}")
            br.close()
    finally:
        if had:
            shutil.copy2(bak, UPD_FILE)
            os.remove(bak)
        elif os.path.exists(UPD_FILE):
            os.remove(UPD_FILE)


def compose_updates():
    """把列表页 / 详情页两张截图拼成一张对比图。"""
    from playwright.sync_api import sync_playwright

    blocks = [("① 进列表页：这本书被排到第一位，封面左下角压了一条「有更新」并描了暖色边；"
               "工具条右侧还有「N 部作品有更新」与一键清空", "u1_list.png"),
              ("② 点进去：顶部「本次新增」区块直接点名是哪一卷（含所属子目录），"
               "并可当场下载", "u2_detail.png"),
              ("③ 同一个页面往下看：那个有新卷的分组挂着「有更新 +1」并自动展开，"
               "卷行带「新」标 —— 新卷常常落在默认折叠的靠后分组里，不这样等于没提示",
               "u3_group.png")]
    html = ("""<!doctype html><meta charset="utf-8"><style>
 body{margin:0;padding:26px;background:#f5f2ed;color:#22282f;
      font-family:"Microsoft YaHei UI","Segoe UI",system-ui,sans-serif}
 h1{font-size:19px;margin:0 0 4px} .sub{color:#6b7280;font-size:13px;margin-bottom:18px}
 .box{background:#fff;border:1px solid #e3ded6;border-radius:12px;padding:12px;
      width:956px;margin-bottom:18px}
 .box img{width:932px;display:block;border-radius:8px;border:1px solid #eee}
 .box p{font-size:13px;margin:10px 2px 2px;line-height:1.6}
</style>
<h1>「新增卷」提示 —— 真实浏览器渲染结果</h1>
<div class="sub">书里多了一卷之后：列表页置顶 + 封面角标 → 点进去点名是哪一册 → 回到列表恢复原位。
（截图用的是真实书库与真实卷号，状态为演示用的人造数据）</div>"""
            + "".join(f'<div class="box"><img src="{f}"><p>{t}</p></div>' for t, f in blocks))
    p_html = os.path.join(OUT, "_updates.html")
    with open(p_html, "w", encoding="utf-8") as f:
        f.write(html)
    with sync_playwright() as p:
        br = p.chromium.launch(channel="msedge", headless=True)
        pg = br.new_page(viewport={"width": 1008, "height": 1200}, device_scale_factor=1)
        pg.goto("file:///" + p_html.replace("\\", "/"), timeout=8000)
        pg.wait_for_timeout(700)
        pg.screenshot(path=os.path.join(OUT, "compare_updates.png"), full_page=True)
        br.close()
    os.remove(p_html)


def main():
    os.makedirs(OUT, exist_ok=True)
    import importlib.util
    if importlib.util.find_spec("playwright") is None:
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
        probe_updates()
        compose_updates()
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
    print(f"  产物：{os.path.join(OUT, 'compare.png')} / compare_updates.png")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
