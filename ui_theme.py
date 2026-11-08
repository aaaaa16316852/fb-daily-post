"""
ui_theme.py — 歸仁土地仲介 工具共用介面樣式
（介面美化部門維護；各程式共用同一份，要換風格只改這裡）
"""
import html
import re

import streamlit as st

try:  # streamlit-shadcn-ui（GitHub 約 1.1k★）；沒裝到也能用原生卡片
    import streamlit_shadcn_ui as _ui
except Exception:  # noqa: BLE001
    _ui = None

_CSS = """
<style>
.block-container{padding-top:2rem;max-width:1100px}
/* 標題橫幅（與謄本系統同款藍紫漸層） */
.hero{margin-bottom:.6rem}
.hero h1{font-size:1.75rem;margin:0;font-weight:800;
  background:linear-gradient(90deg,#2563eb,#7c3aed);-webkit-background-clip:text;
  background-clip:text;color:transparent}
.hero p{color:#475569;margin:.3rem 0 0;font-size:.92rem}
/* 按鈕：加大、圓角，手機好按 */
.stButton>button,.stDownloadButton>button{border-radius:10px;font-weight:600;min-height:46px}
/* 原生數字卡 */
div[data-testid="stMetric"]{background:#fff;border:1px solid #e2e8f0;padding:12px 16px;border-radius:12px}
/* 流程步驟條 */
.steps{display:flex;gap:6px;margin:.4rem 0 1rem;flex-wrap:wrap}
.step{flex:1;min-width:88px;text-align:center;padding:8px 4px;border-radius:10px;font-size:.85rem;
  background:#eef2f7;color:#64748b;border:1px solid #e2e8f0}
.step.done{background:#dbeafe;color:#1d4ed8;border-color:#bfdbfe}
.step.now{background:linear-gradient(90deg,#2563eb,#7c3aed);color:#fff;border-color:transparent;
  font-weight:700;box-shadow:0 2px 8px rgba(37,99,235,.25)}
/* FB 貼文預覽卡 */
.fbcard{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:16px 18px;
  box-shadow:0 1px 3px rgba(15,23,42,.06);max-width:680px}
.fbhead{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.fbava{width:42px;height:42px;border-radius:50%;display:flex;align-items:center;justify-content:center;
  background:linear-gradient(135deg,#2563eb,#7c3aed);color:#fff;font-weight:800;font-size:1.1rem}
.fbname{font-weight:700;color:#0f172a;font-size:.95rem}
.fbsub{color:#64748b;font-size:.78rem}
.fbbody{word-break:break-word;color:#0f172a;font-size:.95rem;line-height:1.65}
.fbbody .tag{color:#2563eb;font-weight:600}
.fbcomment{margin-top:12px;padding:8px 12px;background:#f1f5f9;border-radius:12px;
  font-size:.85rem;color:#334155;word-break:break-all}
@media (max-width:640px){
  .block-container{padding-left:.8rem;padding-right:.8rem}
  .hero h1{font-size:1.4rem}
  .step{min-width:60px;font-size:.75rem}
}
</style>
"""


def apply_theme():
    """app.py 開頭（set_page_config 之後）呼叫一次。"""
    st.markdown(_CSS, unsafe_allow_html=True)


def hero(title: str, subtitle: str = ""):
    sub = f"<p>{html.escape(subtitle)}</p>" if subtitle else ""
    st.markdown(f'<div class="hero"><h1>{html.escape(title)}</h1>{sub}</div>', unsafe_allow_html=True)


def steps_html(labels: list[str], current: int) -> str:
    """current = 目前步驟索引；-1 = 尚未開始；>= len(labels) = 全部完成。"""
    out = []
    for i, lb in enumerate(labels):
        cls = "done" if i < current else ("now" if i == current else "")
        mark = "✓ " if i < current else ""
        out.append(f'<div class="step {cls}">{mark}{html.escape(lb)}</div>')
    return f'<div class="steps">{"".join(out)}</div>'


def steps(labels: list[str], current: int, slot=None):
    (slot or st).markdown(steps_html(labels, current), unsafe_allow_html=True)


_TAG = re.compile(r"(#[^\s#]+)")


def fb_preview(text: str, name: str, comment: str = "", sub: str = "剛剛 · 🌏 公開"):
    """把貼文顯示成類似 FB 的預覽卡（Hashtag 會變藍色）。"""
    # 換行轉 <br>：HTML 區塊裡不能有空行，否則 Streamlit 會把後半段當 Markdown 解析
    body = _TAG.sub(r'<span class="tag">\1</span>', html.escape(text or "")).replace("\n", "<br>")
    initial = html.escape((name or "歸")[0])
    cmt = f'<div class="fbcomment">💬 {html.escape(comment).replace(chr(10), "<br>")}</div>' if comment else ""
    st.markdown(
        f'<div class="fbcard"><div class="fbhead"><div class="fbava">{initial}</div>'
        f'<div><div class="fbname">{html.escape(name)}</div><div class="fbsub">{html.escape(sub)}</div></div></div>'
        f'<div class="fbbody">{body}</div>{cmt}</div>',
        unsafe_allow_html=True,
    )


def metric_cards(items: list[tuple[str, str, str]], key: str = "mc"):
    """items = [(標題, 數值, 說明), ...]；有 shadcn 用 shadcn 卡片，沒有就用原生 st.metric。"""
    cols = st.columns(len(items))
    for i, (col, (title, value, desc)) in enumerate(zip(cols, items)):
        with col:
            if _ui is not None:
                try:
                    _ui.metric_card(title=title, content=str(value), description=desc, key=f"{key}_{i}")
                    continue
                except Exception:  # noqa: BLE001  套件 API 變動時退回原生
                    pass
            st.metric(title, value, help=desc or None)
