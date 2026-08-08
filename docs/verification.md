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

## 待驗(需要 `TRAVELPAYOUTS_TOKEN`)

拿到 token 後執行:

```bash
python scripts/spike_datasource.py --out ../docs/spike-datasource.md
```

三個關卡:

- **S1a 單程語義** —— 不帶 `return_date` 拿回來的每日價,是不是真的單程價。
  若其實錨在來回票上,拼票加總會系統性高估,而且看起來完全合理。
- **S1b 覆蓋率** —— 冷門機場可能一列都沒有。這些必須顯示成「查無資料」,
  不能讓組合靜默消失。
- **S2 排名一致性** —— 判準是**序**不是**值**。快取價穩定偏移 25% 但序不變,
  可出貨;價差 5% 但序翻掉,產品是死的。
- **S3 速率限制** —— 文件沒寫。實測連續呼叫的 status code 與延遲,盯 429。
