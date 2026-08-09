# 驗證紀錄

這裡記的是**用瀏覽器或真實請求量出來的事實**,不是推論。每一條都附上怎麼重驗。

---

## Google Flights `tfs`:目的地是 field 14,不是 16

**日期**:2026-08-08 · **方法**:Chromium 實際載入候選 URL,讀回 Google 渲染出來的表單

`tfs` 是 Google Flights 把整個搜尋條件塞進網址的 protobuf,格式未公開。網路上
流傳的欄位表把目的地寫成 field 16 —— 用那個編碼產生的連結**看起來是對的**:
Google 會正確進入「多停點」模式、正確帶入出發地與日期,只有目的地是空的
(「要去哪裡?」)。這是最糟的一種錯:連結能開、頁面正常、答案錯。

實測把目的地換成 field 14 之後,兩段行程完全正確帶入:

| 欄位 | 編號 | 說明 |
| --- | ---: | --- |
| 航段 | 3 | repeated,每段一個 |
| 日期 | 2 | `YYYY-MM-DD`,航段內 |
| 出發地 | 13 | 巢狀 Airport,內含 field 2 = IATA |
| **目的地** | **14** | 同上。**不是 16** |
| 乘客 | 8 | repeated varint,一人一個 |
| 艙等 | 9 | 1 經濟 / 2 豪經 / 3 商務 / 4 頭等 |
| 行程類型 | 19 | 2 單程 / 3 多段 |

**重驗方式**:`tests/test_deeplinks.py` 把已驗證的位元組寫死當黃金樣本。若 Google
改版,測試不會發現(它只鎖住我們的編碼沒被亂改),所以連結開起來不對時,
要回到瀏覽器重新推導欄位編號,不要用猜的。

已驗證可用的樣本:

```
https://www.google.com/travel/flights?tfs=GhoSCjIwMjYtMTAtMDVqBRIDVFBFcgUSA05SVBoaEgoyMDI2LTEwLTExagUSA0lUTXIFEgNUUEVAAUgBmAED
→ 多停點:TPE→NRT (10/05)、ITM→TPE (10/11)、1 人、經濟艙
```

---

## Travelpayouts 參考資料不需要 token,價格需要

**日期**:2026-08-08 · **方法**:直接 curl

| 端點 | 結果 |
| --- | --- |
| `/data/countries.json` | 200,253 筆 |
| `/data/cities.json` | 200,9,644 筆 |
| `/data/airports.json` | 200,10,369 筆 |
| `/v1/prices/calendar`(無 token) | **401 Unauthorized** |

所以選單、機場替代群組、國家/城市樹在拿到金鑰之前就完整可用,只有排名要等 token。

---

## 386 個「可飛」的地點其實不是機場

**日期**:2026-08-08 · **方法**:統計 `airports.json` 的 `iata_type`

```
airport 9,261 · railway 699 · bus 184 · heliport 173 · harbour 52
其中 flightable=true 但 iata_type≠airport 的有 386 筆
```

具體案例:`LMJ`(Tokyo Bus Station)`city_code=TYO`、`flightable=true`,只用
`flightable` 過濾的話,它會跟成田、羽田並列出現在「東京」的機場選單裡,然後
被送去查一班不存在的航班。

**重驗方式**:`tests/test_refdata.py::TestPicker::test_bus_stations_never_reach_the_picker`

---

## 回應大小:排名全部,只送出前 50 筆

**日期**:2026-08-08 · **方法**:對正式服務發真實請求

台北 → 東京(2–4 晚)+ 大阪(2–4 晚),出發區間 10/01–10/14:

```
8,232 種組合 · 20 次 API 呼叫 · 回應 18 KB · 0.18 秒
```

每一列帶九個深連結(三個來源 × 整趟 + 每段三個),其中三個要編 protobuf。
若把 8,232 列全部序列化,約是 74,000 個 URL 與一份瀏覽器算不動的 JSON。

**重驗方式**:`tests/test_api.py::TestPayloadSize`

---

## Google Flights 的三種搜尋模式(field 19)

**日期**:2026-08-08 · **方法**:Chromium 載入兩個只差一個位元組的連結,讀回 Google 渲染的模式

| 值 | 模式 | 用在 |
| ---: | --- | --- |
| 1 | Round trip(有回程欄位) | 去回同一組機場的普通來回票 |
| 2 | One way | 單程 |
| 3 | Multi-city | 開口票、多段 |

**為什麼要分**:Google 對來回票與多停點是**不同的計價方式**,來回票價是當成一趟旅程
構造的。把普通來回票送成多停點會高估它 —— 在倒買法的比較裡,那等於系統性偏袒倒買法。
兩者渲染出來的欄位一模一樣,所以這個錯誤不會有任何徵兆。

---

## Google Flights 的航空公司篩選是航段裡的 field 6

**日期**:2026-08-09 · **方法**:Chromium 開一次 TPE→NRT,用 Google 自己的篩選面板
按「僅限長榮」,再把它產生的 URL 讀回來解碼

```
篩選前  field 3 { 2:"2026-10-07"  13{2:"TPE"}  14{2:"NRT"} }
篩選後  field 3 { 2:"2026-10-07"  6:"BR"  13{...}  14{...} }
                              ^^^^^^^^
```

重複出現一次代表一家(`6:"BR" 6:"CI"`),而且**每一段都要設** —— Google 自己的
介面在多停點模式就是這樣寫的。實測 `?tfs=…MgJCUjICQ0k…` 開啟後篩選列顯示
「長榮航空 +1, 航空公司, 已選取」,開口票的多段連結一樣生效。

**Kayak 與 Aviasales 刻意不帶篩選**:Kayak 擋機器人(`/help/bots.html`),
Aviasales 的參數沒驗過。一個錯的篩選參數會讓對方回一張空清單,而空清單長得就像
「那天沒有班機」—— 正好是這個站最不能說錯的那句話。

---

## 沒有一個端點能給「某一天、某條航線、誰飛的單程價」

**日期**:2026-08-09 · **方法**:同一組航線月份打四個端點,數列數與 `airline` 欄位

| 航線・月份 | month-matrix | latest | calendar | cheap / direct |
| --- | ---: | ---: | ---: | ---: |
| OKA→TPE 2026-12 | 10 單程 | 10 單程 | 1 列**全是來回** | 1 列來回 |
| ITM→TPE 2026-12 | 3 單程 | 3 單程 | 6 列全是來回 | 0 |
| KIX→TPE 2026-12 | 28 單程 | 28 單程 | 6 列全是來回 | 0 |
| FUK→TPE 2026-10 | **0** | **0** | 5 列全是來回 | 1 列來回 |

* `month-matrix` 與 `latest` 涵蓋範圍**完全一樣**(日期一模一樣,價差 <0.5%),
  所以換端點救不了缺的價格。
* 帶 `airline` 的只有 calendar / cheap / direct,而那三個在台灣航線上**只回來回票**
  —— 所以「這個單程價是誰飛的」在這個資料源上不存在,不是還沒做。
* `FUK→TPE 2026-10` 整月零列,是使用者回報「還是沒拿到價格」的那一格:
  沒有任何鄰近日期可以推薦,所以舊版就沉默了。現在會說「整個 2026-10 一天都沒有」。

**Flight Search API 沒有權限**:`POST /v1/flight_search` 回 `403 Forbidden`,
而且**故意送錯簽章也是同一個 403** —— 所以那是權限不是編碼,再試簽章變體不會收斂。

---

## 免費的 routes.json 不能拿來當航空公司選單

**日期**:2026-08-09 · **方法**:`api.travelpayouts.com/data/routes.json`(64,964 筆,免 token)

| 航線 | 資料表說有誰飛 |
| --- | --- |
| TPE→NRT | BR CI CX DL JL NH UA |
| TPE→KIX | **3K** BR CI CX **GE** JL MM NH |
| TPE→OKA | CI **GE** |
| **TPE→HND** | (空) |
| **ITM→TPE** | (空) |

`GE` 是已經停業的復興航空,`3K` 是 2025 年停業的捷星亞洲;而**星宇(JX)與
台灣虎航(IT)一條都沒有**,連 TPE→HND 這種每天好幾班的航線都是空的。

所以航空公司選單走**全球清單 + 人工排序**(`zh_names.AIRLINE_ZH`),不受航線限制。
一個把 JX 藏起來、把 GE 端出來的選單,比沒有選單更糟。

---

## 台灣出發的航線拿不到來回票價

**日期**:2026-08-08 · **方法**:`v2/prices/latest` 對照 `one_way=true/false`

| 航線 | `one_way=true` | `one_way=false` |
| --- | ---: | ---: |
| MOW→LED | 100 | 100 |
| LON→PAR | 100 | 11 |
| **TPE→KIX** | 100 | **0** |
| **TPE→NRT** | 100 | **0** |

`v1/prices/cheap`、`v2/prices/week-matrix`、`v1/prices/calendar` 帶 `return_date` 也全部回 0 列。

API 有能力回來回票價,但 Aviasales 的快取是自家使用者餵出來的,台灣航線只有單程。
**倒買法省的就是來回計價,所以它在這個資料源上無法排名** —— 見
`app/reverse.py`,那個模組只組票不報價。

---

## 回應裡的機場代碼是城市碼

**日期**:2026-08-08 · **方法**:逐一請求機場代碼,比對回傳內容

```
請求 TPE→KIX → 回 destination "OSA"
請求 TPE→NRT → 回 destination "TYO"
請求 TSA→NRT → 回 origin      "TPE"
```

但**資料本身是分機場的**,城市碼只是標籤:

```
TPE→NRT (29 列) vs TPE→HND (29 列):交集 0
TPE→KIX (30 列) vs TPE→ITM (14 列):交集 0
TPE→OSA (30 列) vs TPE→KIX (30 列):完全相同 → 城市查詢回的是最便宜那個機場
```

照抄回傳的城市碼當索引,查詢時用機場碼就永遠對不上,整站會在坐擁資料的情況下顯示
「此航段查無資料」。**重驗**:`tests/test_cached.py::TestAirportCodesSurviveTheRoundTrip`

---

## 待驗(需要 `TRAVELPAYOUTS_TOKEN`)

拿到 token 後執行:

```bash
python scripts/spike_datasource.py --out ../docs/spike-datasource.md
```

三個關卡:

- **S1a 單程語義** —— ✅ 已通過(2026-08-08)。month-matrix 回的 30 列 `return_date`
  全是空字串,`number_of_changes: 0`、`duration: 160` 分鐘(TPE→KIX 直飛),
  所以是單程價,拼票加總成立。
  ⚠️ 判準不能用「帶不帶 `return_date` 的價差」—— 該參數被端點忽略,兩者回傳完全相同,
  舊版腳本會從相同的數字推出「所以是來回價」的相反結論。
- **S1b 覆蓋率** —— 冷門機場可能一列都沒有。這些必須顯示成「查無資料」,
  不能讓組合靜默消失。
- **S2 排名一致性** —— 判準是**序**不是**值**。快取價穩定偏移 25% 但序不變,
  可出貨;價差 5% 但序翻掉,產品是死的。
- **S3 速率限制** —— 文件沒寫。實測連續呼叫的 status code 與延遲,盯 429。
