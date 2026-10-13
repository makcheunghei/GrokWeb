"""Only controlled messages cross the MCP/diagnostic boundary."""

MESSAGES = {
    "unsupported_platform": "目前只支援 macOS 本機 Chrome。",
    "invalid_profile": "Chrome profile 必須係單一目錄名稱，例如 Default 或 Profile 1。",
    "profile_missing": "搵唔到指定 Chrome profile 嘅 Cookies 資料庫。",
    "cookie_database": (
        "無法即時讀取 Chrome Cookies 資料庫；請稍後再試，必要時正常關閉 Chrome 再開啟。"
    ),
    "keychain_denied": "無法取得 Chrome Safe Storage 權限；請喺 macOS Keychain 授權後再試。",
    "cookie_decryption": "無法解密 Chrome 嘅 Grok cookie；請確認 Keychain 權限同 Chrome 登入狀態。",
    "auth_required": (
        "請喺指定 Chrome profile 開啟 grok.com，重新登入後再試；工具會自動重新讀取 cookie。"
    ),
    "verification_required": (
        "Grok 拒絕直連或要求網頁驗證；請喺同一 Chrome profile 開啟 grok.com "
        "完成驗證、更新 cookie 後再試。更新後仍可能無法直連。"
    ),
    "rate_limited": "Grok 搜尋配額不足或請求過密；請稍後再試。工具冇自動重送搜尋。",
    "model_unavailable": "指定模型／模式目前不可用；請省略 model 使用網頁預設模式。",
    "models_unavailable": "無法取得 Grok 網頁嘅即時模型／模式列表，未使用固定列表代替。",
    "default_unavailable": (
        "Grok 網頁未提供可確認嘅預設模型／模式；請喺 grok.com 確認可用模式後指定 model。"
    ),
    "timeout": "請求已逾時；工具已停止等候，冇自動重送搜尋。",
    "network_error": "無法連接 Grok；請檢查網絡後再試。",
    "upstream_error": "Grok 回傳錯誤；工具冇自動重送搜尋。",
    "protocol_error": "Grok 回應格式不符合預期，可能網頁協定已更新。",
    "stream_interrupted": "Grok 串流未完整完成；部分內容唔會當成搜尋成功。",
    "search_unconfirmed": "未確認搜尋已執行；普通模型答案唔會當成搜尋結果。",
    "empty_answer": "Grok 搜尋已完成，但未提供答案。",
    "invalid_query": "query 必須係非空白文字，最多 20,000 個字元。",
    "internal_error": "本機工具發生錯誤；診斷已遮罩敏感資料。",
}


class GrokError(Exception):
    def __init__(self, code: str, *, profile: str | None = None):
        self.code = code if code in MESSAGES else "internal_error"
        message = MESSAGES[self.code]
        if profile is not None and self.code == "auth_required":
            message = message.replace("指定 Chrome profile", f"Chrome {profile}")
        super().__init__(f"{self.code}: {message}")


def http_error(status: int, *, models: bool = False) -> GrokError:
    if status == 401 or 300 <= status < 400:
        return GrokError("auth_required")
    if status == 403:
        return GrokError("verification_required")
    if status == 429:
        return GrokError("rate_limited")
    if status == 404:
        return GrokError("models_unavailable" if models else "model_unavailable")
    return GrokError("upstream_error")
