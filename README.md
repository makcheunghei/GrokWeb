# GrokWeb

用本機 Chrome 嘅 Grok 登入狀態，為 MCP 客戶端提供 Web 同原生 X 搜尋。

單一 Python 程序、stdio 連接、自動讀取 cookie。唔需要 API key、CLIProxyAPI、HTTP 服務或管理後台。

## 開始使用

需要 macOS、Google Chrome 同 [uv](https://docs.astral.sh/uv/)。

1. 喺 Chrome `Default` profile 登入 [grok.com](https://grok.com)。
2. 下載專案並安裝依賴：

   ```sh
   git clone https://github.com/makcheunghei/GrokWeb.git
   cd GrokWeb
   uv sync --frozen
   uv run grokweb doctor
   ```

3. macOS 如要求讀取 **Chrome Safe Storage**，請完成 Keychain 授權。工具唔會要求你貼出 cookie。
4. 將 `examples/mcp.json` 嘅 `grokweb` 項目加入支援 stdio 嘅 MCP 客戶端。Codex 設定範例係 `examples/codex.toml`。將 `/absolute/path/to/GrokWeb` 換成你下載專案嘅絕對路徑；如果客戶端搵唔到 `uv`，將 `command` 換成 `command -v uv` 顯示嘅完整路徑。

啟動指令：

```sh
uv run --frozen --directory /absolute/path/to/GrokWeb grokweb serve
```

stdio 服務啟動後等候 MCP 訊息，唔會顯示網頁或命令提示。建議客戶端工具逾時設為 150 秒，容納工具本身嘅 120 秒上限。

指定同一部電腦嘅另一個 Chrome profile：

```sh
uv run grokweb --profile 'Profile 1' doctor
uv run grokweb --profile 'Profile 1' serve
```

亦可設定 `GROKWEB_CHROME_PROFILE`；明確嘅 `--profile` 優先。每個程序只使用指定 profile，唔會掃描或合併其他帳戶。

## 兩個搜尋工具

| 工具 | 參數 | 功能 |
| --- | --- | --- |
| `grok_web_search` | `query`、可選 `model` | 搜尋公開網頁，取得答案及來源 |
| `grok_x_search` | `query`、可選 `model` | 使用 Grok 原生 X 搜尋，取得公開貼文來源 |

`model` 係可選嘅網頁模式 ID，例如 `auto`、`fast` 或 `expert`，唔係官方 API 嘅模型代號。日常搜尋只需提供 `query`；程式會自動取得網頁端當前預設模式。需要升級嘅模式會報錯。如要查看即時模式及權限，可喺本機執行 `grokweb doctor`。

```json
{"query": "搜尋 @NASA 最近公布嘅太空任務消息，附上原帖連結"}
```

日期、語言、帳號等要求直接寫入 `query`，屬搜尋指示，唔係硬性篩選器。`query` 最多 20,000 個字元。

成功結果包含：

```json
{
  "answer": "Grok 整理嘅答案",
  "citations": ["https://example.com/source"],
  "sources": [
    {"url": "https://example.com/source", "title": "來源標題", "type": "web"}
  ]
}
```

X 貼文使用 `type: "x_post"`。`sources` 包含搜尋所得來源，`citations` 只列被引用嘅來源；兩者數量可以不同。Token 用量只喺上游提供可識別嘅 `usage` 時回傳，唔估算。

只有普通模型答案而冇原生搜尋證據，會回傳 `search_unconfirmed`。已執行搜尋但搵唔到結果，可以正常回傳空來源。串流未完成則報錯，唔交付半份結果。

## 登入更新與診斷

每次工具調用都重新讀取 Chrome 資料庫。重新登入後，再調用工具即可，毋須重啟 MCP。

| 錯誤 | 處理方式 |
| --- | --- |
| `auth_required` | 喺指定 Chrome profile 開啟 grok.com，重新登入後再試 |
| `verification_required` | 喺同一 profile 完成網頁驗證並更新 cookie；更新後仍可能無法直連 |
| `keychain_denied` | 完成 macOS Keychain 嘅 Chrome Safe Storage 授權後再試 |
| `cookie_database` | 稍後再試，必要時正常關閉 Chrome 再開啟；工具唔會讀取舊備份 |
| `cookie_decryption` | 檢查 Keychain 權限、Chrome 登入同 cookie 格式相容性 |
| `rate_limited` | 等候帳戶配額恢復，唔自動重試 |
| `model_unavailable` | 省略 `model` 使用網頁預設模式，或喺 grok.com 確認帳戶可用模式 |
| `models_unavailable` / `default_unavailable` | 網頁協定可能有變，或未提供預設模式；唔會用固定列表冒充 |
| `timeout` / `network_error` | 檢查網絡後再試；原搜尋可能已消耗 Grok 配額 |
| `stream_interrupted` / `protocol_error` | 上游串流不完整或格式有變，需要檢查協定相容性 |

安全診斷只顯示模式、錯誤分類同統計：

```sh
uv run grokweb doctor
# 額外執行 Web、X 各一次真實搜尋，會使用你嘅 Grok 額度：
uv run grokweb doctor --search
```

## 資料與執行方式

- 只支援 macOS 本機 Chrome。精確查詢 `grok.com`／`.grok.com` 下嘅登入及驗證 cookie allowlist，唔載入其他網站資料。
- 資料庫以唯讀方式開啟，包含當前 WAL；唔複製整個資料庫，唔使用忽略 WAL 嘅舊快照。過期 cookie 會排除，Chrome v24 網域 hash 亦會驗證。
- Keychain 正常解密後，憑證只經內部管道傳送並留喺記憶體，唔寫入設定、日誌或 Git。每次請求完結即釋放該次連線。
- HTTP 目標固定為 `https://grok.com`，驗證 TLS、禁止跟隨重新導向、唔讀取環境代理設定；支援系統層面 VPN。唔開 HTTP 監聽埠。
- 搜尋使用臨時對話，停用 Grok 記憶、個人化、圖片生成及私人連接器。搜尋問題仍會傳送畀 Grok；臨時模式唔代表上游完全冇保留資料。
- 每個程序同時只執行一個請求；120 秒總上限包含排隊、讀取 cookie、模式查詢及搜尋。支援 MCP 取消，唔自動重送搜尋。
- 網頁端點並非穩定官方 API。cookie-only 直連被拒絕時會報錯，唔啟動瀏覽器或切換至付費 API。

## 開發與驗證

```sh
uv sync --frozen
uv run pytest
uv run ruff check .
uv build
```

一般測試使用合成 cookie、臨時 SQLite 同模擬 HTTP／MCP 連線，唔會讀取真實登入資料或消耗 Grok 額度。真實搜尋只經明確執行 `doctor --search` 或 MCP 搜尋調用。

協定參考：

- [Grok_Search_Mcp](https://github.com/MapleMapleCat/Grok_Search_Mcp)：搜尋工具嘅設計參考；此專案獨立實作，冇引入原專案管理後台或程式碼。
- [Grok 網頁](https://grok.com)：即時模式由 `POST /rest/modes` 讀取；搜尋使用 `POST /rest/app-chat/conversations/new`。
- [Grok 網頁 API bundle](https://cdn.grok.com/_next/static/chunks/30dsyl0b4415p.js)：搜尋結果、引用及 NDJSON envelope schema。
- [Grok 網頁 stream handler](https://cdn.grok.com/_next/static/chunks/3dvqp1t3jzl6a.js)：以 `isSoftStop` 確認完成，繼續讀取最終 snapshot；資源 URL 隨部署而變。
