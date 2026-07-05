# AI Session 對話檢視器

把 Claude Code CLI 的 session 紀錄（`~/.claude/projects/<專案>/<sessionId>.jsonl`）與 Codex session 紀錄
（`~/.codex/sessions/**/*.jsonl`）轉成
方便「翻閱」的 **HTML** 與方便 grep／存檔的 **Markdown**。零外部相依，只用 Python 3 標準函式庫，
**跨平台**（Windows / Debian / macOS），並支援**多個 Claude 帳號**與 Codex 來源。

## 快速開始

Windows（PowerShell）：
```powershell
py ai_session_viewer.py            # 全部帳號/專案 -> .\out，同時輸出 HTML + Markdown
py ai_session_viewer.py --open     # 轉換後自動打開 out\index.html
py ai_session_viewer.py --no-codex --open      # 只轉 Claude（預設會連 ~/.codex 一起轉）
```
或直接**雙擊 `run.cmd`**。

Debian / macOS：
```bash
python3 ai_session_viewer.py --open
# 或： chmod +x run.sh && ./run.sh
```

打開 `out\index.html`：session 索引，可用**關鍵字搜尋**、**專案下拉**、**月份下拉**三者交叉篩選，
點欄位標題可排序，右側顯示符合筆數；點標題進入單一對話。
HTML 把工具呼叫、思考、子代理對話都做成**可摺疊**區塊，預設收合，閱讀時不被雜訊淹沒。
若某 Claude 專案有 memory，索引與對話頁會出現 🧠 入口連到該專案的 memory 頁（見〈專案 memory〉）。

## 常用參數

| 參數 | 說明 |
|------|------|
| `--claude-source <路徑>` | 輸入的 Claude `projects` 目錄，可**多次指定**，亦可寫成 `標籤=路徑`；覆寫自動偵測 |
| `--account <名稱>` | 捷徑：等同 `--claude-source 名稱=~/.claude-名稱/projects`（預設帳號用 `--account default`） |
| `--codex-source <路徑>` | 指定 Codex `sessions` 目錄或單一 JSONL，可多次指定，亦可寫成 `標籤=路徑`；覆寫自動偵測 |
| `--no-claude` | 不讀取 Claude Code 紀錄 |
| `--no-codex` | 不讀取 Codex 紀錄（預設會自動偵測 `~/.codex/sessions`） |
| `--out <路徑>` | 輸出資料夾（預設 `./out`） |
| `--format html\|md\|both` | 輸出格式（預設 `both`） |
| `--project <字串>` | 只轉名稱含此字串的專案，例如 `--project Obts` |
| `--include-empty` | 連同只有 `/指令`、無實際對話的空 session 一起輸出 |
| `--force` | 忽略快取，全部重新產生 |
| `--open` | 完成後自動打開 `index.html` |

## 多帳號 / 多工具來源

呈現多個來源的通用方式就是**指定輸入目錄與輸出目錄**：用一個或多個 `--claude-source` 指向各自的 `projects` 目錄，
輸出到同一個 `--out`。索引頁會多一個「帳號」欄與下拉篩選，各來源的輸出會依工具與帳號分開，
Claude Code 會放在 `out/sessions/claude-code/<標籤>/`，Codex 會放在 `out/sessions/codex/<標籤>/`。

```bash
# 明確指定多個來源（適合作業系統多使用者、或紀錄放在非預設位置）
python3 ai_session_viewer.py \
  --claude-source main=~/.claude/projects \
  --claude-source work=~/.claude-work/projects \
  --out ~/ai-logs --open
```

只轉 Codex（停掉 Claude 即可，Codex 預設自動偵測）：

```bash
python3 ai_session_viewer.py --no-claude --out ~/codex-logs --open
```

未加任何來源參數時，**Claude 與 Codex 都會自動偵測**：`~/.claude*` 下所有含 `projects` 的目錄、以及 `~/.codex/sessions`（沒有 `projects` 子目錄的、
例如某些第三方工具的設定夾，會自動略過）。若你的多帳號剛好是 `~/.claude` 與 `~/.claude-<名稱>` 這種命名，
也可以用捷徑 `--account <名稱>`。

> **關於多帳號與 `--account`：** Claude Code 沒有官方的多帳號功能，常見做法是進 CLI 前先設
> `CLAUDE_CONFIG_DIR` 指向另一個設定資料夾（例如 Windows `set CLAUDE_CONFIG_DIR=%USERPROFILE%\.claude-work`、
> Unix `export CLAUDE_CONFIG_DIR=~/.claude-work`），各帳號的 `projects` 就分屬不同資料夾。
> `--account work` 只是「`~/.claude-work/projects`」的捷徑，**前提是你的設定夾照 `~/.claude-<名稱>` 命名**；
> 若 `CLAUDE_CONFIG_DIR` 指到其他位置（例如 `D:\foo`），請改用 `--claude-source work=D:\foo\projects`。

## 增量建置（只重產有變動的）

每次產生會在 `out\.build-manifest.json` 記錄每個 session 來源檔的指紋（mtime + 大小，含外部子代理檔）。
下次執行時只會重產**有變動或新增**的 session，其餘直接沿用既有輸出；索引頁每次都會重建（很快）。
已刪除的 session 其輸出檔會自動清掉。完成訊息會顯示「新建/更新 N、沿用 M」。

- 要強制全部重來：加 `--force`。
- 局部重產（`--project` / `--account` / `--claude-source`）時，不會動到也不會誤刪其他範圍的既有輸出。

## 輸出結構

```
out/
├─ index.html      ← 翻閱用：可搜尋 / 篩選 / 排序的總索引
├─ index.md        ← grep / 存檔用
├─ cache-report.html ← 快取分析報告（有可分析的 Claude 相鄰步驟/長閒置資料時產生，index 頂有連結）
├─ cache-report.md   ← 同上，grep / 存檔用
├─ sessions/
│  ├─ claude-code/ ← 工具 namespace
│  │  ├─ main/     ← 每個來源(帳號)一個資料夾
│  │  │  └─ 20260529-1456__ProjectA__1672668c.{html,md}
│  │  └─ work/
│  │     └─ 20260601-1030__ProjectB__fe081c1a.{html,md}
│  └─ codex/
│     └─ default/
│        └─ 20260615-1452__ProjectC__019eca0d.{html,md}
│                    檔名為  日期__專案__sessionId前8碼
└─ memory/         ← 各 Claude 專案的 memory 檢視（該專案有 memory/ 才產生）
   └─ claude-code/
      └─ main/
         └─ -home-…-ProjectA-<hash>.html   檔名為  munged專案名-完整munged的sha1前16碼
```

## 它怎麼處理紀錄

- **依時間排序**呈現，並把新版格式中被拆成多筆事件的 assistant 回合（thinking / 文字 / 工具）
  併回單一回合；工具的輸出會附在對應的工具呼叫底下。
- 過濾掉 `/指令`、本機指令輸出、系統提醒等雜訊；只有這些內容的 session 預設不輸出。
- **專案命名**：以 Claude 的 munged 夾（`~/.claude/projects/<munged-cwd>/`）為單位，**一夾一專案名**，
  顯示名取自該夾「原生 cwd」的葉名。專案搬遷後把舊夾 session 複製進新夾，舊 session 也會歸到新專案名底下；
  單頁仍如實顯示它當時的 `cwd／帳號／夾`，不抹掉歷史。
- 標題優先序：你用 `/rename` 設的名稱（索引以 ✎ 標示，並把 AI 自動標題列為輔助小字）＞
  AI 自動標題（`ai-title`）＞ 第一句使用者訊息。
- 顯示每個 session（與每一則）用到的**模型、估算花費、快取命中率、脈絡/產出 token**（見下方〈成本與快取〉）。
- 訊息或工具結果中的**內嵌圖片**（base64）會以 `<img>` 呈現（超過上限只放佔位）；
  Markdown 連結只允許 http/https/mailto/相對/錨點等安全 scheme，其餘降為純文字。
- 子代理（subagent）對話：同檔內的 `isSidechain` 事件、或外部
  `<sessionId>/subagents/*.jsonl`，都會收進該 session 的「子代理對話」摺疊區。

## 成本與快取（估算）

很多人搞不清「這段對話有沒有命中快取、花了多少」。本工具把它算給你看：

- **索引頁**：每個 session 一欄「估算$」，頁頂顯示**估算總花費**。
- **單一對話頁**：頁頂有「💲估算花費 · ⚡快取命中% · 脈絡峰值 · 產出」；每一則 assistant 還有可勾選的小徽章
  （⚡快取% / 💲花費 / 脈絡 / 產出，預設顯示快取與花費，勾選狀態記在瀏覽器）。
- **Markdown** 也會在 session 表頭與每則標題附上同樣資訊。

怎麼算的（重要）：JSONL 只記 **token 數、不記金額**。金額是**估算**——各模型單價 ×（快取讀取 0.1×、
寫入 1.25×、假設 5 分鐘 TTL）。價格表在程式頂端 `PRICE_PER_M`，會隨官方調價變動，需要時自己改即可；
未知模型只計已知部分並標 `+?`，內部偽模型 `<synthetic>` 視為不計費。

> 名詞：`input`=未命中快取的新輸入；`cache_read`=從快取重用（長對話每輪都重讀整段脈絡，故總量很大）；
> `cache_creation`=寫入快取；「脈絡」≈ 單輪 input+cache，約等於「重開這個 session 第一句要重讀多少」。

**逐步快取命中率**：一個 assistant「回合」常是一次提問內 Claude 連續呼叫多次 API（思考→工具→再思考…）。
多步回合會在每步開頭標出**該步**的 ⚡快取% / 脈絡 / 產出，看得到快取從第一步到最後一步逐步升溫
（沿用同一組勾選開關）。此為 Claude 專屬——Codex 的紀錄結構不適用，只顯示整段彙總。

## 快取分析報告

命中率常常一冷一熱，看單則不容易看出規律。`out/cache-report.html`（index 頂有連結，有可分析的 Claude
相鄰步驟/長閒置資料時才產生；報告頂端「涵蓋範圍」會標出本次納入的 session 與日期區間——縮範圍建置時就是該子集）
把這些 session 的逐次呼叫拿來統計：

- **快取能撐多久（有效 TTL）**：拿同一 session 內相鄰兩次呼叫的「間隔」對上「下一步是否冷啟」，
  分間隔級距算冷啟比例，並標出**仍命中的最久間隔**與**超過約多久後多半冷啟**，再列出**存活最久的命中**幾筆
  （含發生時間）。官方說 prompt cache 約 5 分鐘，但 Claude Code 對不同前綴會用 5 分鐘或 **1 小時**兩種 TTL，
  所以你常會看到撐超過 1 小時仍命中的例外——報告會把這些攤出來。
- **你都什麼時段重新暖機**：把各帳號活動軸上「閒置 ≥ 30 分後、且該步**確實冷啟**的第一步」當成一次快取過期，
  做成 24 小時直方圖、**平日／假日分開**，並再拆「**含／不含 session 第一句**」兩組：session 第一句的冷啟避不掉
  （全新前綴），**不含第一句的才是可避免的**（沒閒置過久就能續用快取）。通常會看到集中在早上開工與午休後第一次。
- **過期 vs 伺服器時段（實驗性）**：依 **UTC（伺服器時間）**、只取間隔落在 5 分–1 時的相鄰步驟，看各 UTC 時段的
  冷啟率，測「全球尖峰（13–21 UTC）是否較易過期」。跨帳號匯總；樣本受你作息偏置、N 偏小，僅供探索。

純估算、只統計 Claude 主對話（子代理／Codex 不納入）；時間用本機時區。資料存在每個 session 的
manifest row 裡，所以增量建置不必重讀 JSONL 就能更新報告。

## 專案 memory

Claude Code 的 **memory**（`~/.claude/projects/<專案>/memory/`：`MEMORY.md` 索引＋每則一個 `.md`、含 frontmatter）
會被整理成**每個專案一頁**的檢視（`out/memory/claude-code/<帳號>/<munged專案名>-<hash>.html`）：`MEMORY.md` 當總覽，
每則事實做成一張卡（type 標籤＋描述＋渲染內文；`[[name]]` 與 `MEMORY.md` 內的連結都轉成同頁錨點，點了就跳）。

三個入口，**只有該 Claude 專案真的有 `memory/` 時才出現**：

- **索引下拉**：在「專案」下拉選到該專案時，表格上方浮出 🧠 面板連到它的 memory 頁。
- **索引每列**：屬於該專案的每個 session 列尾有一個 🧠。
- **對話頁頂**：每頁左上「← 回索引」旁有「🧠 專案 memory」。

只有 Claude 有這種 memory（Codex 無，所以「選 Claude」是天然條件）；memory 頁每次重建，並清掉已不存在專案的孤兒頁。

## 全文搜尋

索引頁的搜尋比對 標題／專案／cwd／夾(munged)／帳號／分支 等欄位（所以搬遷前的舊路徑或舊專案名仍搜得到）。要搜**對話內容**，直接對輸出的 `.md` 用 ripgrep：

```bash
rg "關鍵字" out/        # 或用 VS Code 全域搜尋
```

## 開發 / 測試

附一支不含任何真實對話的煙霧測試（自動造假 session 跑轉換器，驗證表格、工具摺疊、
圖片內嵌、`/rename` 標題、模型/token、連結安全等）：

```bash
python tests/test_smoke.py      # 成功印 OK
pytest tests/                   # 或用 pytest
```

## 已知簡化

- Markdown 算繪是夠用版：支援標題、清單、粗斜體、行內碼、程式碼區塊、引用、連結、表格；
  巢狀清單會被攤平，極少數複雜語法可能不完美。原始 `.md` 輸出則是原文，不受影響。
- 若曾在對話中倒帶重編輯，被捨棄的分支會按時間一併顯示（不會遺漏內容）。
```
