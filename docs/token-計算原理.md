# Token 與成本：從 JSONL 看懂計算原理

這份文件用「**JSONL 裡實際看得到的欄位**」帶你理解：token 是什麼、一輪對話的 token 怎麼組成、
快取（prompt caching）為什麼讓數字看起來很大卻很便宜、以及金額是怎麼從 token 估出來的。
所有概念都對應到本工具（`ai_session_viewer.py`）的實際算法，附真實可觀察的範例。

> 速讀：**JSONL 只記 token 數，不記金額。** 金額是「估算」＝token × 單價 ×（快取倍率）。
> 看不懂時先記住一句話：「`input` 是這輪**新算**的、`cache_read` 是這輪**重用**的（很便宜）、`output` 是**產出**的。」

---

## 0. 基礎觀念：token 是什麼

- **token** 是模型處理文字的最小計費單位，約等於「一小段字」。英文約 1 token ≈ 4 個字元、
  ≈ 0.75 個英文單字；中文大致 1 個字 ≈ 1～2 token。
- 模型**讀進去的**（你的提問、系統提示、先前對話、工具結果）算 **input（輸入）token**；
  模型**寫出來的**（回答、思考、工具呼叫參數）算 **output（產出）token**。
- 計費是**輸入和輸出分開算、單價不同**（輸出通常貴好幾倍）。
- 每一次 API 呼叫（你送一則、模型回一則）都會回報一組 token 用量，這就是 JSONL 裡的 `usage`。

---

## 1. JSONL 裡實際看得到什麼：`usage` 欄位

每則 assistant 訊息的 `message.usage` 長這樣（Claude Code）：

```json
"usage": {
  "input_tokens": 1200,
  "cache_creation_input_tokens": 18000,
  "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 18000},
  "cache_read_input_tokens": 164000,
  "output_tokens": 350
}
```

主要欄位的**語意**（這是最常被誤解的地方）：

| 欄位 | 意思 | 計費 |
|---|---|---|
| `input_tokens` | 這輪**未命中快取的新輸入**（注意：**不是**整段脈絡，是扣掉快取後的剩餘） | 全價 |
| `cache_creation_input_tokens` | 這輪**寫進快取**的量（第一次看到、存起來下次用） | 5 分 TTL 1.25×／1 小時 TTL 2×（見 §3） |
| `cache_creation`（物件） | 上一列的 **TTL 細分**：寫進 5 分鐘快取、1 小時快取各多少（新版才有） | — |
| `cache_read_input_tokens` | 這輪**從快取重用**的量（之前存過，直接讀） | 約 0.1× 輸入價 |
| `output_tokens` | 模型**產出**的量（回答＋思考＋工具參數） | 輸出價 |

> ⚠️ 關鍵：Claude 的 `input_tokens` **已經扣掉快取部分**。很多人以為 input 是「整個脈絡」，其實不是。
> 整段脈絡要把三個輸入欄位**加起來**（見下一節）。

---

## 2. 一輪對話的 token 組成：「脈絡」怎麼算

一輪實際送進模型的**完整輸入**（也就是模型那一刻「看到多少東西」）是：

```
這輪脈絡 = input_tokens + cache_creation_input_tokens + cache_read_input_tokens
```

接著模型產出 `output_tokens`。所以：

```
這輪總 token = 脈絡(三個輸入相加) + output
```

本工具就是這樣算的（`_collect_usage`）：

```python
i  = input_tokens                  # 新輸入
c1 = cache_creation_input_tokens   # 寫快取
c2 = cache_read_input_tokens       # 讀快取
o  = output_tokens                 # 產出
total_in = i + c1 + c2             # 這輪脈絡
```

**「脈絡峰值」（ctx peak）** ＝整個 session 裡單輪 `total_in` 的最大值，約等於
「如果重開這個對話、第一句話模型要重讀多少東西」。

---

## 3. 為什麼 `cache_read` 那麼大、`input` 那麼小？——Prompt Caching 原理

這是看 JSONL 最反直覺的地方：長對話裡 `cache_read` 動輒十幾萬，`input` 卻只有幾百。原因是 **prompt caching**。

### 機制

- 模型**沒有記憶**。每送一輪，整個對話歷史（系統提示＋前面所有來回＋工具結果）都要**重新送一次**給模型讀。
- 第二輪開始，前面那一大段其實**和上一輪一模一樣**。與其每輪都全價重算，API 會把它**快取**起來：
  - 第一次看到 → 記為 `cache_creation`（寫入，稍貴，約 1.25×）。
  - 後續每輪重用 → 記為 `cache_read`（讀取，超便宜，約 0.1×）。
- 真正「這輪才新增」的（你剛打的字、剛回來的工具結果）才算 `input_tokens`（全價）。

### 所以你會在 JSONL 看到的典型樣貌

```
第 1 輪： input=2500   cache_create=0      cache_read=0       output=300
第 2 輪： input=120    cache_create=2800   cache_read=2500    output=250
第 8 輪： input=80     cache_create=1500   cache_read=164000  output=400
```

愈到後面，`cache_read` 愈大（整段歷史都在重讀），但因為只算 0.1×，其實很省。
**這就是為什麼「token 數字很嚇人、花費卻沒那麼多」。**

### 快取有期限（TTL）

快取不是永久的，有存活時間（TTL），且**每次使用會刷新**：
- **5 分鐘 TTL**：寫入約 **1.25×**。
- **1 小時 TTL**：寫入約 **2×**（存比較久所以寫入較貴）。

新版 JSONL 的 `usage.cache_creation` 物件**會區分**兩種 TTL 各寫了多少
（`ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens`），本工具依細分**逐筆精算**
（實測近期 Claude Code 幾乎全用 1 小時 TTL 寫入）。舊資料沒有這個細分，該部分按 5 分鐘（1.25×）估，
若實際是 1 小時快取會**低估**。

---

## 4. 快取命中率（cache %）怎麼解讀

```
快取命中率 = cache_read / (input + cache_create + cache_read)
           = cache_read / 這輪脈絡
```

- **高（80–95%）**：很正常、很好——表示大部分脈絡都從快取便宜重用，沒有重複全價付費。
- **低或 0%**：通常是該 session 第一輪，或距上次超過 TTL 導致快取失效、要重新寫入。

這是一個**比值**，分子分母會同步放大，所以即使 token 數字本身有誤差，命中率仍相對穩定。

### 整段 vs 每一步：一個「回合」其實是多次 API 呼叫的合計

檢視器裡每個 🤖 標頭上的 `⚡xx%` 是**整個回合的彙總值**——但一個「回合」往往**不是一次** API 呼叫。
你提一個問題後，Claude 為了完成任務常會「思考 → 叫工具 → 看結果 → 再思考 → 再叫工具 …」，
**每一步都是一次獨立的 API 呼叫**（各有自己的 `message.id` 與 `usage`，所以各有自己的快取命中率）。
中間夾的 `tool_result`（純 user 事件）**不另起回合**，要到最後一步「純文字答覆、不再叫工具」才換你說話——
這就是為什麼一個回合會被切成好幾步（見第 5 節的 `message.id`）。

> 關鍵：有沒有命中率，看的是「是不是一次 API 呼叫」，**不是「有沒有叫工具」**。
> 最後那筆純文字答覆同樣是獨立一步、有自己的命中率。工具只是讓回合被切成多步的原因。

為什麼每一步的命中率會不一樣？因為**脈絡是逐步累積的**：每多走一步，前面所有思考／工具結果都進了快取，
下一步重讀時 `cache_read` 就更大、命中率更高。真實一例（同一個回合、9 步）：

```
步驟1 ⚡61%   步驟2 ⚡65%   步驟3 ⚡68%   步驟4 ⚡80%
步驟5 ⚡94%   步驟6 ⚡92%   步驟7 ⚡93%   步驟8 ⚡98%   步驟9 ⚡94%
```

命中率從 61% 一路「熱」到 9 成以上，這個過程被單一彙總值藏起來了。所以檢視器在**多步回合**裡
會於每步開頭插一條分隔列（`步驟 N ⚡% · ctx · ↑`），單步回合則省略（彙總＝那一步，不必重複）。
每步的 `ctx`／`↑` 沿用頁面上「脈絡／產出」勾選開關，預設只顯示 `⚡%`。

> 彙總值仍有意義：它的 `⚡%` 是把整個回合三個輸入欄位**先各自加總再相除**，
> 約等於「這個回合整體有多少比例靠快取省下來」；逐步值則告訴你「省的過程長怎樣」。

---

## 5. 為什麼要「依 message.id 去重」

新版 Claude Code 會把**同一則** assistant 回應，在 JSONL 裡**拆成多筆事件**
（思考一筆、文字一筆、每個工具呼叫各一筆），但它們**共用同一個 `message.id` 和同一份 `usage`**。

如果你逐筆事件把 `usage` 加總，同一則回應的 token 就會被**重複計算好幾次**。
正確做法是**依 `message.id` 去重**——同一個 id 只算一次：

```python
if mid and mid in seen:
    continue          # 這則的 usage 已經算過了，跳過
seen.add(mid)
```

> 這也是「為什麼有去重邏輯」的原因。Codex 有一個更棘手的版本，見第 7 節。

---

## 6. 從 token 到金額：成本公式

金額不在 JSONL 裡，是**估算**出來的。每次呼叫：

```
成本(USD) = ( input          × 輸入單價
            + 寫快取(5 分)   × 輸入單價 × 1.25    ← ephemeral_5m（無細分的舊資料也按此估）
            + 寫快取(1 小時) × 輸入單價 × 2.00    ← ephemeral_1h
            + cache_read     × 輸入單價 × 0.10    ← 讀快取
            + output         × 輸出單價 ) ÷ 1,000,000
```

- **單價**是「每 100 萬（1M）token」的美金價，依模型不同。例如 Claude Opus ＝ 輸入 \$5／輸出 \$25。
- 三個輸入欄位**共用輸入單價**，只是快取部分各乘自己的倍率；寫入倍率依 `cache_creation` 物件的
  TTL 細分逐筆套用（見 §3）。
- 除以 1,000,000 是因為單價是「每 1M token」。

### 實例（Opus，輸入 \$5／輸出 \$25）

某則：`input=1000`、`cache_create=20000`（細分顯示全為 1 小時 TTL）、`cache_read=180000`、`output=500`

```
= (1,000×5  +  20,000×5×2.0  +  180,000×5×0.10  +  500×25) ÷ 1,000,000
= (5,000    +  200,000       +  90,000          +  12,500)  ÷ 1,000,000
= 307,500 ÷ 1,000,000
= $0.3075
```

注意：雖然 `cache_read` 高達 18 萬 token，但因為只算 0.1×，它的成本（\$0.09）反而比
2 萬 token 的 `cache_create`（\$0.20）還低——**這就是快取的威力**。

### 估算的已知限制

1. JSONL 只記 token、不記錢 → 金額純估算。
2. TTL 細分（`cache_creation` 物件）只有新版資料才有；沒有細分的舊資料一律當 5 分（1.25×），
   若實際是 1 小時快取會低估。
3. 用公告定價，沒算批次折扣／企業折扣。
4. 未知模型（沒有單價）只計已知部分並標 `+?`；完全沒價就標「不估價」。

---

## 7. Codex 的不同（為什麼需要特別處理）

Codex（OpenAI）的 JSONL 在 `event_msg` 的 `token_count.info.last_token_usage` 記用量，
語意和 Claude **有三個關鍵差異**：

1. **`input_tokens` 含快取**：Codex 的 input 是「整段輸入（含 cached）」，不像 Claude 已扣掉。
   所以要 `input − cached_input_tokens` 才等於 Claude 語意的「新輸入」：
   ```python
   "input_tokens":      max(total_in - cached, 0),   # 還原成「未命中的新輸入」
   "cache_read_input_tokens": cached,
   ```
   否則 `total_in = input + cache_read` 會把快取**重複算一次**。

2. **`output_tokens` 已含 reasoning**：Codex 的 `reasoning_output_tokens` 是 `output_tokens` 的
   **子集**，不能再另外加，否則產出會被高估。

3. **會發重複的 `token_count`**：Codex 在**同一次回應**前後常發出**多筆完全相同**的 token_count
   （`function_call → token_count → function_call_output → 相同 token_count`）。若無腦累加，
   同一回應會被算兩次，實測整體 token 會**膨脹約 2×**。
   解法是用 `(message_id, usage 簽章)` 去重——同一則已掛過的相同用量就跳過：
   ```python
   if mid and sig is not None and sig not in attached_usage.setdefault(mid, set()):
       attach(...)              # 沒掛過才掛
       attached_usage[mid].add(sig)
   ```

4. **沒有「逐步快取命中率」**：第 4 節「整段 vs 每一步」是 Claude 專屬。Claude 一次 API 呼叫的
   多筆事件**共用同一個 `message.id`** 且每筆都帶 usage，所以能乾淨地切出步驟、逐步顯示命中率。
   Codex 不行：每個事件（reasoning／message／工具呼叫）都被塞**獨立合成 id**（會把一個回合切成
   上百個「步驟」），而 usage 只掛在某一筆（多數步驟會是空白）。因此 Codex **只保留整段彙總的快取%，
   不畫逐步分隔列**（`analyze()` 對 Codex 以 `per_step=False` 呼叫 `group_turns()`）。

> 因為目前沒有可靠的 OpenAI 公告價可填，Codex **只顯示 token、不估金額**（標「不估價」）。
> token 與整段快取% 仍照常顯示。

---

## 8. 名詞速查表

| 名詞 | 一句話 |
|---|---|
| token | 計費最小單位，約「一小段字」 |
| input（輸入） | 模型讀進去的 token |
| output（產出） | 模型寫出來的 token |
| `input_tokens` | （Claude）未命中快取的**新**輸入；（Codex）含快取的整段輸入 |
| `cache_creation` | 這輪**寫進**快取的 token（5 分 1.25×／1 小時 2×；同名物件記 TTL 細分） |
| `cache_read` | 這輪**從快取重用**的 token（約 0.1×，便宜） |
| 脈絡（context） | 這輪的完整輸入 ＝ input + cache_create + cache_read |
| 脈絡峰值 | 整個 session 單輪脈絡的最大值 |
| 快取命中率 | cache_read ÷ 脈絡，愈高愈省 |
| 步驟（step） | 回合內的一次 API 呼叫（一個 message.id），各有自己的脈絡與命中率 |
| 回合（turn） | 你一次提問後、到下次換你說話之前的所有步驟合計 |
| TTL | 快取存活時間（5 分 / 1 小時，每次使用會刷新），影響寫入倍率 |
| message.id 去重 | 同一則被拆多筆事件，靠 id 只算一次 |

---

## 9. 動手觀察：自己打開 JSONL 看

不必靠工具，你可以直接撈原始 `usage` 來對照理解。Claude 紀錄在
`~/.claude/projects/<專案>/<sessionId>.jsonl`，每行是一個 JSON 事件。

```python
import json
from pathlib import Path

path = Path.home() / ".claude/projects/<專案>/<sessionId>.jsonl"
seen = set()
for line in path.read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    o = json.loads(line)
    msg = o.get("message") or {}
    if msg.get("role") != "assistant":
        continue
    mid = msg.get("id")
    if mid in seen:           # 依 message.id 去重
        continue
    seen.add(mid)
    u = msg.get("usage") or {}
    i  = u.get("input_tokens", 0)
    c1 = u.get("cache_creation_input_tokens", 0)
    c2 = u.get("cache_read_input_tokens", 0)
    o_ = u.get("output_tokens", 0)
    ctx = i + c1 + c2
    hit = round(100 * c2 / ctx) if ctx else 0
    print(f"新輸入={i:>6} 寫快取={c1:>6} 讀快取={c2:>7} 產出={o_:>5} | 脈絡={ctx:>7} 命中={hit:>3}%")
```

你會看到：第一輪命中 0%、`input` 大；之後 `cache_read` 暴增、命中率衝到 9 成、`input` 變小。
這就是前面講的全部原理，在你自己的資料上活生生跑一遍。

---

## 延伸：本工具相關程式碼位置

- `PRICE_PER_M` / `CACHE_WRITE_MULT` / `CACHE_WRITE_MULT_1H` / `CACHE_READ_MULT`：單價與快取倍率。
- `call_cost()` / `_ephemeral_split()`：第 6 節的成本公式與 TTL 細分讀取。
- `_collect_usage()`：第 2、5 節的彙整與 message.id 去重。
- `_step_usage()` / `group_turns()` 的 `_step` 標記 / `render_step_meters()`：第 4 節「整段 vs 每一步」的逐步命中率。
- `_codex_usage()` / `_codex_usage_sig()` / `load_codex_session()` 的 `token_count` 分支：第 7 節 Codex 處理。
