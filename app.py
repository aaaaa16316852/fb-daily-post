"""
app.py — 粉專每日文章生成系統（介面層）
密碼登入 → 一鍵生成 → 即時顯示 AI 團隊進度 → 人工確認（發布／手動修改／重新生成）
"""
import hmac
import json
import os
from datetime import datetime

import streamlit as st

import core
import ui_theme as ui

st.set_page_config(page_title="粉專每日文章生成｜歸仁土地仲介", page_icon="📝", layout="wide")
ui.apply_theme()

HISTORY_FILE = "history.json"  # Streamlit Cloud 重啟會清空，記得定期下載備份
NODE_LABEL = {
    "topic": "🧭 出題者找熱門關鍵字",
    "writer": "✍️ 撰寫者寫初稿",
    "judge": "⚖️ 雙評審評分中（Gemini + Claude）",
    "reviser": "🛠️ 修改者依缺點改寫",
    "finalize": "📦 定稿",
    "human_review": "👀 等待人工確認",
}
STEP_LABELS = ["🧭 出題", "✍️ 撰寫", "⚖️ 雙評審", "🛠️ 修改", "👀 人工確認"]
# 某個節點「做完」之後，步驟條要亮到哪一格
STEP_AFTER = {"topic": 1, "writer": 2, "judge": 2, "reviser": 2, "finalize": 4, "human_review": 5}


# ───────────────────────── 登入 ─────────────────────────
def check_password() -> bool:
    if st.session_state.get("auth_ok"):
        return True
    ui.hero("📝 粉專每日文章生成系統", "歸仁土地仲介｜丘正其")
    pw = st.text_input("請輸入密碼", type="password")
    if st.button("登入", type="primary"):
        if hmac.compare_digest(pw, str(st.secrets.get("APP_PASSWORD", ""))):
            st.session_state.auth_ok = True
            st.rerun()
        else:
            st.error("密碼錯誤")
    return False


if not check_password():
    st.stop()


# ───────────────────────── 初始化 ─────────────────────────
@st.cache_resource(show_spinner=False)
def get_config() -> core.Config:
    return core.Config.from_secrets(st.secrets)


cfg = get_config()
ss = st.session_state
ss.setdefault("graph", None)
ss.setdefault("thread", None)
ss.setdefault("phase", "idle")      # idle / review / published
ss.setdefault("edit_mode", False)
ss.setdefault("error", "")

if ss.graph is None:
    ss.graph = core.build_graph(cfg)  # 每個使用者 session 一份（含 MemorySaver）


def load_history() -> list[dict]:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_history(item: dict):
    h = load_history() + [item]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False, indent=2)


def state_values() -> dict:
    if not ss.thread:
        return {}
    return ss.graph.get_state(ss.thread).values or {}


# ───────────────────────── 執行圖 ─────────────────────────
def run_graph(inp, start_step: int = 0):
    ss.error = ""
    seen = len(state_values().get("log", []))
    step_slot = st.empty()
    ui.steps(STEP_LABELS, start_step, step_slot)
    with st.status("AI 團隊工作中…", expanded=True) as box:
        try:
            for ev in ss.graph.stream(inp, ss.thread, stream_mode="updates"):
                for node, upd in ev.items():
                    if node == "__interrupt__":
                        continue
                    box.update(label=NODE_LABEL.get(node, node) + "…")
                    ui.steps(STEP_LABELS, STEP_AFTER.get(node, start_step), step_slot)
                    log = (upd or {}).get("log") if isinstance(upd, dict) else None
                    if log:
                        for line in log[seen:]:
                            st.write(line)
                        seen = len(log)
        except Exception as e:  # noqa: BLE001
            box.update(label="❌ 發生錯誤", state="error")
            ss.error = f"{type(e).__name__}: {e}"
            ss.phase = "idle"
            return
        box.update(label="完成，請人工確認", state="complete", expanded=False)

    snap = ss.graph.get_state(ss.thread)
    if "human_review" in (snap.next or ()):
        ss.phase = "review"
    elif (snap.values or {}).get("status") == "published":
        ss.phase = "published"


# ───────────────────────── 側邊欄 ─────────────────────────
history = load_history()
today = datetime.now(core.TW_TZ).strftime("%Y-%m-%d")
today_done = [h for h in history if h.get("date") == today]

with st.sidebar:
    st.header("⚙️ 設定")
    st.caption(f"Gemini：{' → '.join(cfg.gemini_models)}")
    st.caption(f"Claude：{' → '.join(cfg.claude_models)}")
    st.caption(f"過關分數 {cfg.pass_score}｜最多 {cfg.max_rounds} 輪")
    if not cfg.license_no:
        st.warning("尚未設定 LICENSE_NO（經紀業證號），簽名檔會缺證號。")
    if not cfg.ai_assistant_url:
        st.info("尚未設定 AI_ASSISTANT_URL，第一則留言不會產生。")

    hint = st.text_input("指定方向（可空白）", placeholder="例：農地繼承、土銀土地貸款")
    extra_avoid = st.text_area("近期已發題目（一行一個，避免重複）", height=120)

    st.divider()
    st.subheader("📚 發文紀錄")
    st.caption(f"共 {len(history)} 篇｜今天 {len(today_done)} 篇")
    for h in reversed(history[-10:]):
        st.caption(f"{h['date']}｜{h['title']}")
    if history:
        st.download_button("下載紀錄 JSON", json.dumps(history, ensure_ascii=False, indent=2),
                           file_name="fb_post_history.json", mime="application/json")
    if st.button("登出"):
        ss.clear()
        st.rerun()


# ───────────────────────── 主畫面 ─────────────────────────
ui.hero("📝 粉專每日專業知識文", "出題者 → 撰寫者 → 雙評審（Gemini + Claude）→ 修改者（最多 3 輪）→ 人工確認")

if ss.error:
    st.error(f"執行失敗：{ss.error}")
    st.caption("若是 429 額度問題，備援模型也用完了，請稍後再試或到 secrets 調整模型清單。")

if ss.phase == "idle":
    ui.steps(STEP_LABELS, -1)
    if today_done:
        st.warning(f"今天已經產生過：「{today_done[-1]['title']}」。一天一篇，確定要再產生嗎？")
    if st.button("🚀 生成今日文章", type="primary", width="stretch"):
        avoid = [h["title"] for h in history] + [x.strip() for x in extra_avoid.splitlines() if x.strip()]
        ss.thread = core.new_thread()
        ss.edit_mode = False
        run_graph(core.initial_state(avoid, hint))
        st.rerun()


def render_rounds(v: dict):
    hist = v.get("history", [])
    if not hist:
        return
    st.subheader("📊 每輪分數")
    rows = []
    for h in hist:
        g, c = h["gemini"], h["claude"]
        rows.append({
            "輪次": h["round"],
            "Gemini": g.get("score") if g.get("ok") else "失敗",
            "Claude": c.get("score") if c.get("ok") else "失敗",
            "決策分(取低)": h["score"],
            "錯字": "有" if h["has_typo"] else "無",
            "缺點數": len(h["feedback"]),
            "結果": "✅ 過關" if h["passed"] else "❌ 未過",
        })
    st.dataframe(rows, hide_index=True, width="stretch")

    st.subheader("🧾 修改紀錄")
    for h in hist:
        with st.expander(f"第 {h['round']} 版：{h['title']}（決策分 {h['score']}）"):
            cols = st.columns(2)
            for col, (name, r) in zip(cols, (("Gemini", h["gemini"]), ("Claude", h["claude"]))):
                with col:
                    if r.get("ok"):
                        bd = r.get("breakdown", {})
                        st.markdown(f"**{name}（{r.get('model')}）：{r['score']} 分**")
                        st.caption(f"法規 {bd.get('law')}/30｜吸引力 {bd.get('appeal')}/25｜"
                                   f"完整 {bd.get('completeness')}/25｜可讀性 {bd.get('readability')}/20")
                    else:
                        st.markdown(f"**{name}：評審失敗**")
                        st.caption(r.get("error", ""))
            st.markdown("**具體缺點（交給修改者）**")
            if h["feedback"]:
                for i, f in enumerate(h["feedback"], 1):
                    st.write(f"{i}. {f}")
            else:
                st.write("無")
            st.text_area("本版全文", h["full_text"], height=250, disabled=True,
                         key=f"round_text_{h['round']}_{v.get('regen_count', 0)}")

    with st.expander("🔧 執行紀錄"):
        for line in v.get("log", []):
            st.caption(line)


if ss.phase == "review":
    v = state_values()
    hist = v.get("history", [])
    best = next((h for h in hist if h["full_text"] == v.get("final_text")), hist[-1] if hist else {})

    ui.steps(STEP_LABELS, 4)
    ui.metric_cards([
        ("決策分數", best.get("score", "-"), "兩位評審取較低分"),
        ("跑了幾輪", len(hist), f"最多 {cfg.max_rounds} 輪"),
        ("品質門檻", "✅ 達標" if best.get("passed") else "⚠️ 未達標", f"過關分數 {cfg.pass_score}"),
    ], key="review_mc")
    if not best.get("passed"):
        st.warning("已跑滿輪數仍未達標，請特別檢查下方最後一輪的缺點清單再決定。")

    st.subheader("📄 最終稿")
    if ss.edit_mode:
        edited = st.text_area("直接修改內容", v.get("final_text", ""), height=520, key="edit_box")
        e1, e2 = st.columns(2)
        if e1.button("💾 儲存修改並發布", type="primary", width="stretch"):
            run_graph(core.resume_cmd("edit", text=edited), start_step=4)
            ss.edit_mode = False
            st.rerun()
        if e2.button("取消修改", width="stretch"):
            ss.edit_mode = False
            st.rerun()
    else:
        ui.fb_preview(v.get("final_text", ""), cfg.agent_title, v.get("first_comment", ""))
        with st.expander("📋 純文字版（右上角可複製）"):
            st.code(v.get("final_text", ""), language=None)
        regen_hint = st.text_input("重新生成時的方向（可空白）", key="regen_hint")
        b1, b2, b3 = st.columns(3)
        if b1.button("✅ 確認發布", type="primary", width="stretch"):
            run_graph(core.resume_cmd("approve"), start_step=4)
            st.rerun()
        if b2.button("✏️ 手動修改", width="stretch"):
            ss.edit_mode = True
            st.rerun()
        if b3.button("🔄 重新生成", width="stretch"):
            run_graph(core.resume_cmd("regenerate", hint=regen_hint))
            st.rerun()

    if v.get("first_comment"):
        st.subheader("💬 第一則留言（AI 特助連結）")
        st.code(v["first_comment"], language=None)

    render_rounds(v)


if ss.phase == "published":
    v = state_values()
    if not ss.get("saved_thread") == ss.thread["configurable"]["thread_id"]:
        hist = v.get("history", [])
        save_history({
            "date": today,
            "time": datetime.now(core.TW_TZ).strftime("%H:%M"),
            "title": v.get("final_text", "").split("\n", 1)[0],
            "keywords": v.get("topic", {}).get("keywords", []),
            "scores": [h["score"] for h in hist],
            "text": v.get("final_text", ""),
        })
        ss.saved_thread = ss.thread["configurable"]["thread_id"]

    ui.steps(STEP_LABELS, 5)
    st.success("✅ 已確認！複製下方內容貼到粉專（右上角有複製按鈕）。")
    st.subheader("👀 發布預覽")
    ui.fb_preview(v.get("final_text", ""), cfg.agent_title, v.get("first_comment", ""))
    st.subheader("📄 貼文內容（複製用）")
    st.code(v.get("final_text", ""), language=None)
    if v.get("first_comment"):
        st.subheader("💬 發文後立刻貼到第一則留言")
        st.code(v["first_comment"], language=None)
    st.download_button("下載貼文 .txt", v.get("final_text", ""), file_name=f"fb_post_{today}.txt")

    render_rounds(v)

    if st.button("回到首頁"):
        ss.phase = "idle"
        ss.thread = None
        st.rerun()
