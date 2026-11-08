"""
core.py — 粉專每日文章生成系統（邏輯層）
LangGraph 閉環：出題者 → 撰寫者 → 雙評審（Gemini + Claude）→ 修改者 → … → 定稿 → 人工確認

設計重點
- 簽名檔、固定 Hashtag 由程式組合，不交給模型 → 保證零錯字、格式固定
- 雙評審取「較低分」作為決策分數（保守），任一評審抓到【錯字】一律不過關
- 修改者只收具體缺點清單，不收籠統評語
- Gemini / Claude 都有 429 自動換備援模型
- OpenCC：程式直接抓簡體字（不花 token）
- Langfuse：記錄每次模型呼叫的 token 與費用（沒填金鑰就自動略過）
"""
from __future__ import annotations

import json
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

TW_TZ = timezone(timedelta(hours=8))


# ════════════════════════════════════════════════════════════
# 設定
# ════════════════════════════════════════════════════════════
@dataclass
class Config:
    gemini_api_key: str
    anthropic_api_key: str
    gemini_models: list[str] = field(default_factory=lambda: ["gemini-3.8-flash", "gemini-3.5-flash"])
    claude_models: list[str] = field(default_factory=lambda: ["claude-sonnet-5-5", "claude-haiku-5-5"])
    pass_score: int = 85
    max_rounds: int = 3
    use_search: bool = True  # Gemini 出題/撰寫/評審時用 Google 搜尋查熱門與法規
    # 簽名檔
    agent_title: str = "歸仁土地仲介｜丘正其"
    specialty: str = "專營台南市土地：農地、建地、工業地買賣"
    phone: str = "0955-028-309"
    store: str = "幸福家不動產 台南裕忠加盟店"
    company: str = "鴻運通不動產開發有限公司"
    license_no: str = "(113)登字第468181號"
    fixed_hashtags: list[str] = field(default_factory=lambda: [
        "#歸仁土地仲介", "#台南土地", "#台南農地", "#台南建地", "#台南工業地", "#幸福家不動產"])
    ai_assistant_url: str = "https://www.myhomes.com.tw/mo/ai_qna/16612"
    first_comment_template: str = "🤖 AI特助24hr服務：有土地問題隨時問👉 {url}"
    # Langfuse（記帳本）：三個都留空 = 不啟用
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"
    # OpenCC 誤判白名單：台灣正體也會用、但會被當成簡體的字
    opencc_whitelist: str = ""

    @classmethod
    def from_secrets(cls, s: Any) -> "Config":
        def g(key, default=None):
            try:
                return s[key] if key in s else default
            except Exception:
                return default

        def as_list(v, default):
            if v is None or v == "":
                return default
            if isinstance(v, str):
                return [x.strip() for x in re.split(r"[,\s]+", v) if x.strip()]
            return list(v)

        base = cls(
            gemini_api_key=g("GEMINI_API_KEY") or g("GOOGLE_API_KEY", ""),
            anthropic_api_key=g("ANTHROPIC_API_KEY", ""),
        )
        base.gemini_models = as_list(g("GEMINI_MODELS"), base.gemini_models)
        base.claude_models = as_list(g("CLAUDE_MODELS"), base.claude_models)
        base.pass_score = int(g("PASS_SCORE", base.pass_score))
        base.max_rounds = int(g("MAX_ROUNDS", base.max_rounds))
        base.use_search = str(g("USE_SEARCH", "true")).lower() not in ("false", "0", "no")
        base.license_no = g("LICENSE_NO", base.license_no)
        base.ai_assistant_url = g("AI_ASSISTANT_URL", base.ai_assistant_url)
        tags = as_list(g("FIXED_HASHTAGS"), base.fixed_hashtags)
        base.fixed_hashtags = [t if t.startswith("#") else f"#{t}" for t in tags]
        base.langfuse_public_key = g("LANGFUSE_PUBLIC_KEY", "") or ""
        base.langfuse_secret_key = g("LANGFUSE_SECRET_KEY", "") or ""
        base.langfuse_host = g("LANGFUSE_HOST", base.langfuse_host) or base.langfuse_host
        base.opencc_whitelist = g("OPENCC_WHITELIST", "") or ""
        return base

    def signature(self) -> str:
        lines = [
            "━━━━━━━━━━━━━━",
            "🔍 Google搜尋「歸仁土地仲介」",
            f"👤 {self.agent_title}",
            f"🏞️ {self.specialty}",
            f"📞 {self.phone}",
            f"🏢 {self.store}",
            f"經紀業名稱：{self.company}",
        ]
        if self.license_no:
            lines.append(f"證號：{self.license_no}")
        return "\n".join(lines)

    def first_comment(self) -> str:
        if not self.ai_assistant_url:
            return ""
        return self.first_comment_template.format(url=self.ai_assistant_url)


# ════════════════════════════════════════════════════════════
# 模型呼叫（含 429 備援）
# ════════════════════════════════════════════════════════════
class AllModelsFailed(RuntimeError):
    pass


def _status_code(e: Exception) -> Optional[int]:
    for attr in ("code", "status_code", "status"):
        v = getattr(e, attr, None)
        if isinstance(v, int):
            return v
    m = re.search(r"\b(4\d\d|5\d\d)\b", str(e))
    return int(m.group(1)) if m else None


def _is_fallback_error(e: Exception) -> bool:
    """429 額度、503/529 忙線、404 模型不存在 → 換下一個模型"""
    code = _status_code(e)
    s = str(e).upper()
    return code in (404, 429, 500, 502, 503, 529) or any(
        k in s for k in ("RESOURCE_EXHAUSTED", "UNAVAILABLE", "OVERLOADED", "RATE_LIMIT", "NOT_FOUND"))


class Tracer:
    """Langfuse 記帳本。沒填金鑰、套件沒裝、連線失敗 → 全部安靜略過，不影響發文。"""

    def __init__(self, cfg: Config):
        self.lf = None
        if not (cfg.langfuse_public_key and cfg.langfuse_secret_key):
            return
        try:
            from langfuse import Langfuse
            kw = dict(public_key=cfg.langfuse_public_key, secret_key=cfg.langfuse_secret_key)
            try:
                self.lf = Langfuse(**kw, base_url=cfg.langfuse_host)
            except TypeError:  # 舊版 SDK 參數叫 host
                self.lf = Langfuse(**kw, host=cfg.langfuse_host)
        except Exception:  # noqa: BLE001
            self.lf = None

    trace_id: str = ""  # 同一篇文章的所有呼叫掛在同一個 trace 底下

    @property
    def on(self) -> bool:
        return self.lf is not None

    def new_article(self):
        """新的一篇開始：產生新的 trace_id，之後的呼叫都歸到這篇"""
        self.trace_id = ""
        if not self.lf:
            return
        try:
            self.trace_id = self.lf.create_trace_id()
        except Exception:  # noqa: BLE001
            self.trace_id = ""

    def record(self, name: str, model: str, prompt: str, output: str, usage: dict, seconds: float):
        if not self.lf:
            return
        kw = dict(as_type="generation", name=name, model=model, input=prompt[:6000])
        try:
            try:
                cm = self.lf.start_as_current_observation(
                    **kw, trace_context={"trace_id": self.trace_id}) if self.trace_id \
                    else self.lf.start_as_current_observation(**kw)
            except TypeError:  # SDK 不支援 trace_context → 退回各自一筆
                cm = self.lf.start_as_current_observation(**kw)
            with cm as gen:
                gen.update(output=output[:6000], usage_details=usage,
                           metadata={"seconds": round(seconds, 1)})
                try:
                    gen.update_trace(name="粉專文章")
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass

    def flush(self):
        if self.lf:
            try:
                self.lf.flush()
            except Exception:  # noqa: BLE001
                pass


class LLM:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._gemini = None
        self._claude = None
        self.events: list[str] = []  # 模型切換紀錄，給介面顯示
        self.tracer = Tracer(cfg)
        self.usage: dict[str, int] = {"input": 0, "output": 0}  # 本次生成累計 token

    def _add_usage(self, tag: str, model: str, prompt: str, text: str, u: dict, t0: float):
        self.usage["input"] += u.get("input", 0)
        self.usage["output"] += u.get("output", 0) + u.get("output_thinking", 0)
        self.tracer.record(tag, model, prompt, text, u, time.time() - t0)

    # ---------- Gemini ----------
    def _gem(self):
        if self._gemini is None:
            from google import genai
            self._gemini = genai.Client(api_key=self.cfg.gemini_api_key)
        return self._gemini

    def gemini(self, prompt: str, system: str = "", json_mode: bool = False,
               search: bool = False, temperature: float = 0.7, tag: str = "gemini") -> tuple[str, str]:
        from google.genai import types

        search = search and self.cfg.use_search
        last_err: Exception | None = None
        for attempt in range(2):  # 全部模型都 429 時，等一下再整輪試一次
            for model in self.cfg.gemini_models:
                use_tools = search
                for _ in range(2):  # 搜尋工具不支援時，同模型去掉工具再試
                    kwargs: dict[str, Any] = dict(temperature=temperature)
                    if system:
                        kwargs["system_instruction"] = system
                    if use_tools:
                        kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]
                    elif json_mode:
                        kwargs["response_mime_type"] = "application/json"
                    try:
                        t0 = time.time()
                        r = self._gem().models.generate_content(
                            model=model, contents=prompt,
                            config=types.GenerateContentConfig(**kwargs))
                        text = r.text or ""
                        if not text.strip():
                            raise RuntimeError("empty response")
                        um = getattr(r, "usage_metadata", None)
                        u = {k: int(getattr(um, f, 0) or 0) for k, f in (
                            ("input", "prompt_token_count"),
                            ("output", "candidates_token_count"),
                            ("output_thinking", "thoughts_token_count"),
                            ("input_tool", "tool_use_prompt_token_count"))} if um else {}
                        u["input"] = u.get("input", 0) + u.pop("input_tool", 0)
                        self._add_usage(tag, model, prompt, text, u, t0)
                        return text, model
                    except Exception as e:  # noqa: BLE001
                        last_err = e
                        if use_tools and _status_code(e) == 400:
                            use_tools = False
                            self.events.append(f"⚠️ {model} 搜尋工具失敗，改用無搜尋模式")
                            continue
                        if _is_fallback_error(e) or "empty response" in str(e):
                            self.events.append(f"⚠️ Gemini {model} 失敗（{_status_code(e) or '空回應'}），換備援")
                            break
                        raise
            if attempt == 0:
                time.sleep(6)
        raise AllModelsFailed(f"所有 Gemini 模型都失敗：{last_err}")

    # ---------- Claude ----------
    def _cl(self):
        if self._claude is None:
            import anthropic
            self._claude = anthropic.Anthropic(api_key=self.cfg.anthropic_api_key)
        return self._claude

    def claude(self, prompt: str, system: str = "", temperature: float = 0.2,
               tag: str = "claude") -> tuple[str, str]:
        last_err: Exception | None = None
        for attempt in range(2):
            for model in self.cfg.claude_models:
                try:
                    t0 = time.time()
                    msg = self._cl().messages.create(
                        model=model, max_tokens=3000, temperature=temperature,
                        system=system or "You are a helpful assistant.",
                        messages=[{"role": "user", "content": prompt}])
                    text = "".join(getattr(b, "text", "") for b in msg.content)
                    us = getattr(msg, "usage", None)
                    u = {"input": int(getattr(us, "input_tokens", 0) or 0),
                         "output": int(getattr(us, "output_tokens", 0) or 0)}
                    self._add_usage(tag, model, prompt, text, u, t0)
                    return text, model
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    if _is_fallback_error(e):
                        self.events.append(f"⚠️ Claude {model} 失敗（{_status_code(e)}），換備援")
                        continue
                    raise
            if attempt == 0:
                time.sleep(6)
        raise AllModelsFailed(f"所有 Claude 模型都失敗：{last_err}")


def parse_json(text: str) -> dict:
    """容錯解析：去掉 ```json 圍欄、抓第一個 { 到最後一個 }"""
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    try:
        return json.loads(t)
    except Exception:
        pass
    a, b = t.find("{"), t.rfind("}")
    if a != -1 and b > a:
        return json.loads(t[a:b + 1])
    raise ValueError(f"無法解析 JSON：{text[:200]}")


# ════════════════════════════════════════════════════════════
# 提示詞
# ════════════════════════════════════════════════════════════
PERSONA = (
    "你是台南在地資深土地仲介「歸仁土地仲介｜丘正其」的粉專小編，專營台南市農地、建地、工業地買賣。"
    "讀者是想買賣土地的地主、投資客、想蓋農舍或設廠的人。一律使用台灣繁體中文與台灣慣用語。"
)

TOPIC_PROMPT = """今天是 {date}。請用 Google 搜尋，找出「最近在台灣被大量搜尋」且和下列領域有關的關鍵字：
土地／農地／建地／工業地買賣、房地合一稅、土地增值稅、地價稅、遺產贈與、農舍、農地農用、都市計畫、
重劃區、房貸／土地貸款利率、央行信用管制、銀行（可點名，如台銀、土銀、合庫…）、建商或科技廠進駐台南（可點名公司）。

從中挑「一個」最適合寫成專業知識文的題目（不是物件介紹文），優先與台南相關。
{hint}
近期已寫過、請避開的題目：
{avoid}

只回 JSON，不要任何其他文字：
{{
  "title": "文章標題（自然嵌入熱門關鍵字，25 字內，吸睛但不誇大）",
  "keywords": ["熱門關鍵字1", "關鍵字2", "關鍵字3"],
  "why_trending": "為什麼最近熱門（一句話）",
  "angle": "切入角度／讀者痛點",
  "outline": ["重點1", "重點2", "重點3", "重點4"]
}}"""

WRITER_RULES = """【寫作規範】
1. 只寫專業知識文，不得出現任何物件資訊（地號、售價、坪數報價、聯絡看地等）。
2. 法規、稅率、金額、日期必須正確；不確定的數字不要寫，改寫「以主管機關最新公告為準」。不得捏造條文。
3. 零錯字：注意「的/得/地」「在/再」「已/以」「做/作」等常見錯誤，用台灣繁體字。
4. Facebook 不支援 Markdown：不要用 **、##、- 清單符號；小標可用 emoji（如 📌、✅、💡），適量即可。
5. 開頭兩行就要抓住注意力（點出痛點或熱門話題），結尾用一句邀請留言或私訊的話收尾。
6. 內文 600～1000 字，段落短、好滑手機閱讀。
7. 內文不得放任何網址連結。不要寫簽名檔、不要寫電話、不要在內文放 Hashtag（程式會自動加）。
8. hashtags 依內容挑 4～8 個精準標籤（要和熱門關鍵字相關），不要和固定標籤重複：{fixed}
"""

WRITER_PROMPT = """{rules}
【題目】{title}
【熱門關鍵字】{keywords}
【切入角度】{angle}
【大綱】{outline}

請撰寫文章。只回 JSON，不要任何其他文字：
{{"title": "標題", "body": "內文（用 \\n 換行）", "hashtags": ["#標籤1", "#標籤2"]}}"""

REVISER_PROMPT = """{rules}
以下是上一版文章，評審指出了具體缺點。請「逐條」修正這些缺點，其餘寫得好的部分保留，不要整篇重寫成另一篇。

【上一版標題】{title}
【上一版內文】
{body}
【上一版標籤】{hashtags}

【必須修正的具體缺點】
{feedback}

只回 JSON，不要任何其他文字：
{{"title": "標題", "body": "內文（用 \\n 換行）", "hashtags": ["#標籤1", "#標籤2"]}}"""

JUDGE_SYSTEM = (
    "你是嚴格的台灣不動產法規與社群文案評審，熟悉土地法、平均地權條例、農業發展條例、所得稅法（房地合一）、"
    "遺產及贈與稅法、區域計畫法與都市計畫法。你只回 JSON。"
)

JUDGE_PROMPT = """請評審以下 Facebook 粉專文章（含程式自動加上的簽名檔與標籤）。

【評分標準，總分 100】
- law 法規正確性（0-30）：法規、稅率、金額、年限是否正確，有無過時或捏造
- appeal 吸引力（0-25）：標題與開頭是否抓人、是否結合熱門關鍵字、是否有痛點
- completeness 資訊完整（0-25）：讀者看完能否知道該怎麼做，重要例外或注意事項有無遺漏
- readability 可讀性（0-20）：段落、節奏、手機閱讀體驗、用字；有任何錯字此項最多 10 分

【必檢查項目】
1. 逐字檢查錯字、錯別字、簡體字、標點錯誤。每一個錯字都要列在 feedback，開頭寫「【錯字】」，並寫出原文→正確寫法。
2. 是否為專業知識文（出現物件資訊要扣分並列出）。
3. 內文是否有網址（有就列出）。
4. 是否用了 FB 不支援的 Markdown 符號（**、## 等）。
5. 簽名檔應包含：Google搜尋「歸仁土地仲介」、姓名、專營項目、電話、幸福家店名、經紀業名稱、證號。
6. Hashtag 是否精準對應內容。

【feedback 規則】每一條都必須具體：指出「哪一段／哪一句」有什麼問題、應該怎麼改。
禁止籠統評語（例如「可以更吸引人」「內容不錯」）。沒有缺點就給空陣列。

【文章】
{post}

只回 JSON：
{{"breakdown": {{"law": 0, "appeal": 0, "completeness": 0, "readability": 0}}, "score": 0, "feedback": ["具體缺點1", "具體缺點2"]}}"""

MAX_BREAKDOWN = {"law": 30, "appeal": 25, "completeness": 25, "readability": 20}
VAGUE_PATTERNS = re.compile(r"^(整體|內容|文章)?(還不錯|不錯|很好|可以更|建議再|稍嫌|略顯)")


# ════════════════════════════════════════════════════════════
# 狀態
# ════════════════════════════════════════════════════════════
class PostState(TypedDict, total=False):
    date: str
    hint: str
    avoid_titles: list[str]
    topic: dict
    draft: dict            # {"title","body","hashtags"}
    full_text: str
    round: int
    history: list[dict]    # 每輪：round, title, full_text, gemini, claude, score, passed, feedback, action
    feedback: list[str]
    passed: bool
    final_text: str
    first_comment: str
    status: str            # running / awaiting_review / published / regenerate
    log: list[str]
    regen_count: int


# ════════════════════════════════════════════════════════════
# 組文、規則檢查
# ════════════════════════════════════════════════════════════
URL_RE = re.compile(r"(https?://|www\.)\S+|\b\S+\.(com|tw|net|org)(/\S*)?\b", re.I)


def normalize_tags(tags: list[str], fixed: list[str]) -> list[str]:
    out, seen = [], {t.lower() for t in fixed}
    for t in tags or []:
        t = "#" + re.sub(r"[#\s]", "", str(t))
        if len(t) > 1 and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def assemble(draft: dict, cfg: Config) -> str:
    tags = cfg.fixed_hashtags + normalize_tags(draft.get("hashtags", []), cfg.fixed_hashtags)
    body = draft.get("body", "").replace("\\n", "\n").strip()
    return f"{draft.get('title', '').strip()}\n\n{body}\n\n{cfg.signature()}\n\n{' '.join(tags)}"


# 台灣正體也常用、但 OpenCC 簡轉繁會改掉的字 → 不當成簡體字
TW_OK_CHARS = set("台后里干面余只才斗谷周准系松范卷志借吁咸向注表游布占折制致征采了云丰凶出舍朴姜托划夸板回克秋胡御郁杰欲念帘沈丑卜几刮筑蒙霉合")
_CC: Any = None


def _opencc():
    global _CC
    if _CC is None:
        try:
            from opencc import OpenCC
            _CC = OpenCC("s2t")
        except Exception:  # noqa: BLE001
            _CC = False  # 沒裝套件就略過此檢查
    return _CC


def simplified_chars(text: str, extra_ok: str = "") -> list[str]:
    """回傳「簡體字→正體字」清單（不重複），例如 ['发→發']"""
    cc = _opencc()
    if not cc or not text:
        return []
    ok = TW_OK_CHARS | set(extra_ok)
    out, seen = [], set()
    for line in text.splitlines():
        conv = cc.convert(line)
        if len(conv) != len(line):  # 長度不同時改逐字比對
            pairs = [(c, cc.convert(c)) for c in line]
        else:
            pairs = list(zip(line, conv))
        for a, b in pairs:
            if a != b and a not in ok and a not in seen:
                seen.add(a)
                out.append(f"{a}→{b}")
    return out


def rule_check(draft: dict, extra_ok: str = "") -> list[str]:
    """程式硬性檢查（不靠模型）"""
    issues = []
    body = draft.get("body", "")
    sc = simplified_chars(f"{draft.get('title', '')}\n{body}\n{' '.join(draft.get('hashtags', []))}", extra_ok)
    if sc:
        issues.append(f"【錯字】出現簡體字：{'、'.join(sc)}，請全部改成台灣正體字。")
    if URL_RE.search(body):
        issues.append(f"【規則】內文出現網址「{URL_RE.search(body).group(0)}」，連結不可放內文，請刪除。")
    if "**" in body or re.search(r"^#{1,6}\s", body, re.M):
        issues.append("【規則】內文使用了 Markdown 符號（** 或 ##），FB 不會顯示粗體，請改用 emoji 小標。")
    n = len(re.sub(r"\s", "", body))
    if n < 550:
        issues.append(f"【規則】內文只有約 {n} 字，少於 600 字，資訊不足，請補充實例或注意事項。")
    elif n > 1300:
        issues.append(f"【規則】內文約 {n} 字，太長，請精簡到 1000 字左右。")
    if re.search(r"0\d{2,3}-?\d{3}-?\d{3}|LINE", body):
        issues.append("【規則】內文出現電話或 LINE，聯絡資訊只放簽名檔，請從內文刪除。")
    return issues


# ════════════════════════════════════════════════════════════
# 節點
# ════════════════════════════════════════════════════════════
class Nodes:
    def __init__(self, cfg: Config, llm: LLM):
        self.cfg, self.llm = cfg, llm

    def _log(self, state: PostState, *msgs: str) -> list[str]:
        ev, self.llm.events = self.llm.events, []
        stamp = datetime.now(TW_TZ).strftime("%H:%M:%S")
        return state.get("log", []) + [f"{stamp} {m}" for m in ev + list(msgs)]

    # 出題者
    def topic(self, state: PostState) -> dict:
        avoid = state.get("avoid_titles") or []
        hint = state.get("hint", "").strip()
        prompt = TOPIC_PROMPT.format(
            date=state.get("date") or datetime.now(TW_TZ).strftime("%Y-%m-%d"),
            hint=f"使用者指定方向：{hint}" if hint else "",
            avoid="\n".join(f"- {t}" for t in avoid[-30:]) or "（無）",
        )
        self.llm.usage = {"input": 0, "output": 0}  # 新的一篇，從 0 開始算
        self.llm.tracer.new_article()
        text, model = self.llm.gemini(prompt, system=PERSONA, json_mode=True, search=True,
                                      temperature=0.9, tag="出題")
        topic = parse_json(text)
        return {"topic": topic, "round": 0, "history": [], "feedback": [], "passed": False,
                "status": "running",
                "log": self._log(state, f"🧭 出題者（{model}）：{topic.get('title')}｜關鍵字 {topic.get('keywords')}")}

    def _write(self, prompt: str, tag: str = "撰寫") -> tuple[dict, str]:
        text, model = self.llm.gemini(prompt, system=PERSONA, json_mode=True, search=True,
                                      temperature=0.7, tag=tag)
        d = parse_json(text)
        d.setdefault("hashtags", [])
        return d, model

    def _rules(self) -> str:
        return WRITER_RULES.format(fixed=" ".join(self.cfg.fixed_hashtags))

    # 撰寫者
    def writer(self, state: PostState) -> dict:
        t = state["topic"]
        prompt = WRITER_PROMPT.format(
            rules=self._rules(), title=t.get("title", ""), keywords="、".join(t.get("keywords", [])),
            angle=t.get("angle", ""), outline="；".join(t.get("outline", [])))
        draft, model = self._write(prompt)
        return {"draft": draft, "full_text": assemble(draft, self.cfg), "round": 1,
                "log": self._log(state, f"✍️ 撰寫者（{model}）完成第 1 版：{draft.get('title')}")}

    # 雙評審
    def _judge_one(self, who: str, post: str) -> dict:
        prompt = JUDGE_PROMPT.format(post=post)
        try:
            if who == "gemini":
                text, model = self.llm.gemini(prompt, system=JUDGE_SYSTEM, json_mode=True,
                                              search=True, temperature=0.1, tag="評審-Gemini")
            else:
                text, model = self.llm.claude(prompt, system=JUDGE_SYSTEM, temperature=0.1, tag="評審-Claude")
            r = parse_json(text)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:300], "score": None, "feedback": []}
        bd = {k: max(0, min(int(r.get("breakdown", {}).get(k, 0) or 0), mx)) for k, mx in MAX_BREAKDOWN.items()}
        score = sum(bd.values()) if any(bd.values()) else int(r.get("score", 0) or 0)
        fb = [str(x).strip() for x in r.get("feedback", []) if str(x).strip()]
        fb = [x for x in fb if not VAGUE_PATTERNS.match(x)]  # 濾掉籠統評語
        return {"ok": True, "model": model, "score": score, "breakdown": bd, "feedback": fb}

    def judge(self, state: PostState) -> dict:
        post = state["full_text"]
        with ThreadPoolExecutor(max_workers=2) as ex:
            fg = ex.submit(self._judge_one, "gemini", post)
            fc = ex.submit(self._judge_one, "claude", post)
            g, c = fg.result(), fc.result()

        valid = [r["score"] for r in (g, c) if r["ok"]]
        if not valid:
            raise AllModelsFailed(f"兩位評審都失敗：Gemini={g.get('error')}；Claude={c.get('error')}")
        score = min(valid)  # 保守：取較低分

        rules = rule_check(state["draft"], self.cfg.opencc_whitelist)
        feedback = rules + [f"[Gemini] {x}" for x in g.get("feedback", [])] + \
                   [f"[Claude] {x}" for x in c.get("feedback", [])]
        feedback = list(dict.fromkeys(feedback))  # 去重保序
        has_typo = any("【錯字】" in x for x in feedback)
        passed = score >= self.cfg.pass_score and not has_typo and not rules

        rnd = state.get("round", 1)
        notes = []
        if not c["ok"]:
            notes.append(f"⚠️ Claude 評審失敗，本輪只採 Gemini 分數：{c['error'][:120]}")
        if not g["ok"]:
            notes.append(f"⚠️ Gemini 評審失敗，本輪只採 Claude 分數：{g['error'][:120]}")
        entry = {
            "round": rnd, "title": state["draft"].get("title", ""), "full_text": post,
            "gemini": g, "claude": c, "score": score, "passed": passed,
            "has_typo": has_typo, "feedback": feedback,
        }
        msg = (f"⚖️ 第 {rnd} 輪評分：Gemini {g.get('score')}／Claude {c.get('score')} → 決策分 {score}"
               f"{'（有錯字）' if has_typo else ''}{' ✅ 過關' if passed else ' ❌ 未過'}")
        return {"history": state.get("history", []) + [entry], "feedback": feedback, "passed": passed,
                "log": self._log(state, *notes, msg)}

    # 修改者
    def reviser(self, state: PostState) -> dict:
        d = state["draft"]
        prompt = REVISER_PROMPT.format(
            rules=self._rules(), title=d.get("title", ""), body=d.get("body", ""),
            hashtags=" ".join(d.get("hashtags", [])),
            feedback="\n".join(f"{i}. {x}" for i, x in enumerate(state["feedback"], 1)))
        draft, model = self._write(prompt, tag="修改")
        rnd = state.get("round", 1) + 1
        return {"draft": draft, "full_text": assemble(draft, self.cfg), "round": rnd,
                "log": self._log(state, f"🛠️ 修改者（{model}）依 {len(state['feedback'])} 條缺點改寫 → 第 {rnd} 版")}

    # 定稿：選分數最高的一版（若最後一輪反而退步）
    def finalize(self, state: PostState) -> dict:
        hist = state.get("history", [])
        best = max(hist, key=lambda h: (h["passed"], not h["has_typo"], h["score"], h["round"]))
        note = "" if best["round"] == hist[-1]["round"] else f"（第 {hist[-1]['round']} 版退步，改用第 {best['round']} 版）"
        u = self.llm.usage
        cost_note = f"💰 本篇共用 token：輸入 {u['input']:,}／輸出 {u['output']:,}" + \
                    ("（明細已記到 Langfuse）" if self.llm.tracer.on else "")
        self.llm.tracer.flush()
        return {"final_text": best["full_text"], "first_comment": self.cfg.first_comment(),
                "status": "awaiting_review",
                "log": self._log(state, f"📦 定稿：第 {best['round']} 版，決策分 {best['score']}"
                                        f"{'' if best['passed'] else '（未達標，請人工把關）'}{note}",
                                  cost_note)}

    # 人工確認（LangGraph interrupt）
    def human_review(self, state: PostState) -> dict:
        decision = interrupt({"final_text": state["final_text"], "history": state.get("history", [])})
        action = (decision or {}).get("action")
        if action in ("approve", "edit"):
            text = decision.get("text") or state["final_text"]
            return {"final_text": text, "status": "published",
                    "log": self._log(state, "✅ 人工確認發布" + ("（含手動修改）" if action == "edit" else ""))}
        # regenerate
        avoid = (state.get("avoid_titles") or []) + [state.get("topic", {}).get("title", "")]
        return {"status": "regenerate", "hint": decision.get("hint", ""), "avoid_titles": avoid,
                "regen_count": state.get("regen_count", 0) + 1,
                "log": self._log(state, "🔄 人工要求重新生成" + (f"：{decision.get('hint')}" if decision.get("hint") else ""))}


# ════════════════════════════════════════════════════════════
# 圖
# ════════════════════════════════════════════════════════════
def build_graph(cfg: Config):
    n = Nodes(cfg, LLM(cfg))
    g = StateGraph(PostState)
    g.add_node("topic", n.topic)
    g.add_node("writer", n.writer)
    g.add_node("judge", n.judge)
    g.add_node("reviser", n.reviser)
    g.add_node("finalize", n.finalize)
    g.add_node("human_review", n.human_review)

    g.add_edge(START, "topic")
    g.add_edge("topic", "writer")
    g.add_edge("writer", "judge")
    g.add_conditional_edges(
        "judge",
        lambda s: "finalize" if s.get("passed") or s.get("round", 1) >= cfg.max_rounds else "reviser",
        {"finalize": "finalize", "reviser": "reviser"})
    g.add_edge("reviser", "judge")
    g.add_edge("finalize", "human_review")
    g.add_conditional_edges(
        "human_review",
        lambda s: "topic" if s.get("status") == "regenerate" else END,
        {"topic": "topic", END: END})
    return g.compile(checkpointer=MemorySaver())


def new_thread() -> dict:
    return {"configurable": {"thread_id": uuid.uuid4().hex}, "recursion_limit": 60}


def initial_state(avoid_titles: list[str], hint: str = "") -> PostState:
    return PostState(date=datetime.now(TW_TZ).strftime("%Y-%m-%d"), hint=hint,
                     avoid_titles=avoid_titles, log=[], regen_count=0)


def resume_cmd(action: str, text: str = "", hint: str = "") -> Command:
    return Command(resume={"action": action, "text": text, "hint": hint})
