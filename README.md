# 粉專每日文章生成系統（fb-daily-post）

## 檔案結構
```
fb-daily-post/
├── app.py                 # Streamlit 介面：登入、生成、進度、人工確認
├── core.py                # LangGraph 閉環 + 雙評審 + 429 備援 + 簽名檔
├── requirements.txt
├── .gitignore             # 擋掉 secrets.toml、history.json
├── secrets.toml.example   # secrets 範本（不含真實 key，可以上傳）
└── README.md
```

## 流程
出題者（Gemini＋Google 搜尋熱門關鍵字）→ 撰寫者（Gemini）→ 雙評審（Gemini＋Claude 同時評）
→ 決策分 ≥ 85 且無錯字、無規則違反 → 定稿；否則把具體缺點交修改者 → 再評（最多 3 輪）
→ 人工確認：✅ 確認發布／✏️ 手動修改／🔄 重新生成

- 決策分＝兩位評審的「較低分」
- 任一評審列出【錯字】就不算過關
- 簽名檔、固定 Hashtag 由程式加上，不會打錯字
- 程式另外硬性檢查：內文有網址、Markdown 符號、電話／LINE、字數過少或過多
- 跑滿 3 輪仍未過，會挑分數最高的一版給你，並標示「未達標」

## Secrets 要放什麼
照 `secrets.toml.example` 填，必填：`APP_PASSWORD`、`GEMINI_API_KEY`、`ANTHROPIC_API_KEY`、`LICENSE_NO`。
選填：`GEMINI_MODELS`、`CLAUDE_MODELS`、`PASS_SCORE`、`MAX_ROUNDS`、`USE_SEARCH`、`FIXED_HASHTAGS`、`AI_ASSISTANT_URL`。

## 上傳 GitHub
1. GitHub 右上角 ＋ → New repository → 名稱 `fb-daily-post` → Create
2. Add file → Upload files → 拖入 `app.py`、`core.py`、`requirements.txt`、`.gitignore`、`secrets.toml.example`、`README.md` → Commit
3. **絕對不要上傳真實的 secrets.toml**（repo 如果是公開的，key 會被盜用）

## 部署到 Streamlit Cloud
1. share.streamlit.io → Create app → 選 `fb-daily-post` repo、branch `main`、Main file `app.py`
2. Advanced settings → Secrets → 貼上填好的 secrets 內容 → Deploy
3. 之後改 secrets：App 右下 Manage app → Settings → Secrets，存檔後會自動重啟

## 注意
- `history.json`（發文紀錄）放在 Streamlit Cloud 的暫存空間，重啟會清空；側邊欄可下載備份，或把舊題目貼進「近期已發題目」避免重複。
- 跑一篇大約會呼叫模型 4～10 次（看幾輪），Claude 會用到 Anthropic API 額度。
