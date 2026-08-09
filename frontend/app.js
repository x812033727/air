/* air — 多開口機票組合
 *
 * 沒有打包工具、沒有框架:這是一個表單加一張表,再多的機械就只是負擔。
 *
 * 貫穿全檔的規則:**沒有價格的地方一定要說原因。** 後端已經把「問過但沒有」
 * 跟「根本沒問」分成兩種狀態,前端的責任是不要在畫面上把它們併回成一個空格。
 */

const API = "";
const state = {
  countries: [],
  airportsByCountry: new Map(),
  keys: null,
  // 想搭的航空公司(IATA)。
  airlines: new Set(),
  airlineNames: new Map(),
  // 每個訂票網站的實測狀態(語言、會不會照航空公司篩)。後端給的,前端不猜。
  linkInfo: {},
  // 目前畫在畫面上的那一組,給「比一比」重畫時用。
  lastGroup: null,
  lastCurrency: "TWD",
  // 匯率。外站出發那張票用外幣賣,不換算就沒辦法比。
  fx: {},
  fxBase: "TWD",
};

const $ = (selector) => document.querySelector(selector);

/* ---------------------------------------------------------------- fetch */

/* 金鑰存在伺服器上。以前只存瀏覽器,是因為站台公開、伺服器端的金鑰誰都讀得走;
 * 站台上鎖之後那個理由消失了,而「在網頁上按了儲存卻只有這台瀏覽器算數」對使用者
 * 來說跟壞掉沒兩樣 —— 換手機要重填,伺服器端的工具也拿不到。 */
const LEGACY_KEY_STORE = "air.travelpayouts";

async function api(path, options) {
  const response = await fetch(API + path, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = body.detail;
    throw new Error(
      typeof detail === "string" ? detail : detail?.message || `請求失敗(${response.status})`
    );
  }
  return body;
}

async function airportsFor(countryCode) {
  if (!state.airportsByCountry.has(countryCode)) {
    const body = await api(`/api/ref/countries/${countryCode}/airports`);
    state.airportsByCountry.set(countryCode, body.cities);
  }
  return state.airportsByCountry.get(countryCode);
}

/* ------------------------------------------------------------- rendering */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/** 地點選擇:先選國家,再選城市/機場。
 *
 *  原本這個流程有兩個 bug,不是流程本身的錯:
 *  1. **城市被截斷在 12 個**(`cities.slice(0, 12)`)。日本 72 個可飛城市只列得出
 *     12 個,岡山、函館、石垣點不到,而上面的下拉寫著「日本 (77)」—— 看起來只是
 *     排在後面,實際上沒有那個按鈕。現在改成下拉,**全部列出來**,不截斷。
 *  2. **國家下拉的排序**把 147 個英文國名夾在中文國名中間,挪威排在
 *     Abkhazia…Åland Islands 之後。後端已改成「常用 → 有中文名 → 只有英文名」。
 *
 *  下拉之外再留一個打字的入口:知道代碼的人打 KIX 比翻兩層快,而且打「日本」
 *  也算。兩條路並存,誰都不會卡住。 */
async function renderPlace(container, place, onChange) {
  container.replaceChildren();

  const picked = el("div", "picked");
  const paintPicked = () => {
    picked.replaceChildren();
    if (!place.selected.size) {
      picked.append(el("span", "picked__empty", "還沒選"));
      return;
    }
    for (const code of place.selected) {
      const chip = el("button", "chip chip--picked", code);
      chip.type = "button";
      chip.title = "移除";
      chip.append(el("span", "chip__x", "×"));
      chip.addEventListener("click", () => {
        place.selected.delete(code);
        paintPicked();
        onChange();
      });
      picked.append(chip);
    }
  };

  const add = (codes, label) => {
    for (const code of codes) place.selected.add(code);
    if (label) place.label = label;
    paintPicked();
    onChange();
  };

  const row = el("div", "stop__row");

  // ---- 國家 ----
  const countryField = el("label", "field");
  countryField.append(el("span", "field__label", "國家"));
  const country = el("select");
  for (const item of state.countries) {
    const option = el("option", null, `${item.name_zh} (${item.airport_count})`);
    option.value = item.code;
    if (item.code === place.country) option.selected = true;
    country.append(option);
  }
  countryField.append(country);

  // ---- 城市/機場 ----
  const cityField = el("label", "field");
  cityField.append(el("span", "field__label", "城市 / 機場"));
  const city = el("select");
  cityField.append(city);

  const fillCities = async () => {
    city.replaceChildren();
    const placeholder = el("option", null, "選一個…");
    placeholder.value = "";
    city.append(placeholder);
    // **不截斷。** 這正是原本的死路:第 13 名之後的城市根本沒有按鈕,
    // 而畫面上完全沒有一句話說明。
    for (const item of await airportsFor(place.country)) {
      const codes = item.airports.map((a) => a.code);
      const option = el("option", null,
        codes.length > 1 ? `${item.name}(${codes.join(" ")})` : `${item.name} ${codes[0]}`);
      option.value = item.code;
      city.append(option);
    }
  };

  city.addEventListener("change", async () => {
    const chosen = (await airportsFor(place.country)).find((c) => c.code === city.value);
    if (!chosen) return;
    add(chosen.airports.map((a) => a.code), chosen.name);
    city.value = "";
  });

  country.addEventListener("change", async () => {
    place.country = country.value;
    await fillCities();
  });

  row.append(countryField, cityField);

  // ---- 或直接打字 ----
  const typed = el("div", "combo");
  const search = el("input", "combo__input");
  search.type = "search";
  search.placeholder = "或直接打:福岡 / KIX / 日本";
  search.autocomplete = "off";
  const list = el("div", "combo__list");
  list.hidden = true;
  const close = () => { list.hidden = true; };

  let timer = null;
  search.addEventListener("input", () => {
    clearTimeout(timer);
    const q = search.value.trim();
    if (!q) { close(); return; }
    timer = setTimeout(async () => {
      let body;
      try { body = await api(`/api/ref/places?limit=10&q=${encodeURIComponent(q)}`); }
      catch { return; }
      list.replaceChildren();
      if (!body.places.length) list.append(el("div", "picked__empty", `找不到「${q}」`));
      for (const item of body.places) {
        const hit = el("button", "combo__hit");
        hit.type = "button";
        hit.append(el("b", null, item.name));
        hit.append(el("span", "combo__codes", item.airports.map((a) => a.code).join(" ")));
        hit.append(el("span", "combo__country", item.country));
        // mousedown 而不是 click:blur 會先關掉清單,click 就永遠不會發生。
        hit.addEventListener("mousedown", (event) => {
          event.preventDefault();
          add(item.airports.map((a) => a.code), item.name);
          search.value = "";
          close();
        });
        list.append(hit);
      }
      list.hidden = false;
    }, 200);
  });
  search.addEventListener("blur", () => setTimeout(close, 120));
  search.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
  typed.append(search, list);

  paintPicked();
  container.append(picked, row, typed);
  await fillCities();
}

/* --------------------------------------------------------------- results */

/* Intl 在 zh-TW 底下把 TWD 印成單純的 "$",跟美元長得一模一樣。票價旁邊
 * 的幣別不能有歧義,所以自己掛前綴。 */
const SYMBOLS = { TWD: "NT$", JPY: "¥", USD: "US$", EUR: "€", KRW: "₩", HKD: "HK$" };

const money = (value, currency) =>
  value == null
    ? null
    : (SYMBOLS[currency] || `${currency} `) +
      new Intl.NumberFormat("zh-TW", { maximumFractionDigits: 0 }).format(value);

/** 一個沒有價格的航段,為什麼沒有 —— 以及下一步是什麼。
 *
 *  四種情況在畫面上長得都一樣(一個空格),但要做的事完全不同:沒查過要重跑、
 *  整個月都沒有要換機場、只有遠處有價要整趟挪。全部併成「查無資料」等於把
 *  使用者能做的事一起藏起來。同城替代放在最前面,因為它連日期都不用改。 */
function gapDetail(gap, alternatives, currency, label) {
  const box = el("div");
  if (!gap) return box;

  const nearby_ = gap.same_city || [];
  if (nearby_.length) {
    const line = el("div", "alts alts--strong");
    line.append(el("span", "alts__label", "同城的其他機場,同一天就有票"));
    for (const alt of nearby_) {
      line.append(
        el("span", "alts__day",
           `${alt.origin}→${alt.destination} ${money(alt.price, currency)}`)
      );
    }
    box.append(line);
  }

  const days = alternatives || [];
  if (days.length) {
    const line = el("div", "alts");
    line.append(el("span", "alts__label", `${label} ${gap.text}`));
    for (const day of days) {
      line.append(
        el("span", "alts__day",
           `${day.date.slice(5)} ${money(day.price, currency)}` +
           `(${day.days_away > 0 ? "+" : ""}${day.days_away}天)`)
      );
    }
    box.append(line);
  } else {
    // 沒有日子可以推薦的時候,那句理由就是全部的內容 —— 而它正是使用者
    // 看著一格空白、什麼都不知道的那個情況。
    const line = el("div", "alts");
    line.append(el("span", "alts__label", `${label} ${gap.text}`));
    box.append(line);
  }
  return box;
}

/* --------------------------------------------------------------- notices */

const shownNotices = new Set();

/** `topic` collapses different phrasings of the same problem. Both /warm and
 *  /search report a missing token, in different words; showing both just makes
 *  the real message harder to read. */
function showNotice(title, text, tone = "info", topic = null) {
  const key = topic || text;
  if (shownNotices.has(key)) return;
  shownNotices.add(key);

  const notice = el("div", `notice notice--${tone}`);
  notice.append(el("strong", "notice__title", title));
  notice.append(document.createTextNode(text));
  $("#notices").append(notice);
}

/** Warnings about the same underlying problem share a topic. */
function topicOf(warning) {
  if (warning.includes("Travelpayouts token")) return "no-token";
  if (warning.includes("查無任何價格")) return "empty-routes";
  return warning;
}

/* ------------------------------------------------------------------ init */

async function loadStatus() {
  try {
    const health = await api("/api/health");
    const strip = $("#status-strip");
    strip.replaceChildren();

    const pricing = health.config.cached_prices;
    const chip = el(
      "span",
      `chip chip--quiet${pricing ? "" : " chip--alert"}`,
      pricing ? "查價已接上" : "尚未填金鑰"
    );
    strip.append(chip);
    strip.append(el("span", "chip chip--quiet", health.config.currency));

    // 沒有密碼保護的站台不該顯示變更密碼的欄位 —— 那會暗示有一道其實不存在的門。
    $("#password-block").hidden = !health.config.can_change_password;

    $("#colophon-meta").textContent =
      `機場資料 ${health.row_counts.airports.toLocaleString()} 筆 · ` +
      `快取價 ${health.row_counts.price_cache.toLocaleString()} 筆 · ` +
      `即時來源 ${health.config.live_provider}`;

    if (!pricing) {
      showNotice(
        "還沒有價格",
        "打開上方的「查價金鑰」填入 Travelpayouts token,四段航程才會有參考價。" +
          "在那之前站台照樣組得出票,每一張都有連結可以直接去看真價。",
        "info",
        "no-token"
      );
    }
  } catch (error) {
    $("#status-text").textContent = "後端沒有回應";
  }
}

async function renderKeyState() {
  const keys = await api("/api/keys");
  state.keys = keys;
  const node = $("#key-state");

  if (keys.configured) {
    const where =
      { saved: "存在站台", env: "來自伺服器設定檔", request: "本次請求" }[keys.source] ||
      keys.source;
    node.className = "keys__state keys__state--ok";
    node.textContent =
      `已儲存 token ${keys.masked_token}(${where})` +
      (keys.marker ? ` · marker ${keys.marker}` : " · 未填 marker");
  } else {
    node.className = "keys__state";
    node.textContent = "尚未填入,目前沒有價格可以排名";
  }
  $("#key-token").value = "";
  $("#key-marker").value = keys.marker || "";
}

/** 把舊版存在這台瀏覽器裡的金鑰搬到伺服器,搬完清掉本機那份。
 *  使用者當初真的按過儲存,不該因為我改了設計就要他重打一次。 */
async function migrateLegacyKeys() {
  let legacy;
  try {
    legacy = JSON.parse(localStorage.getItem(LEGACY_KEY_STORE) || "{}");
  } catch {
    legacy = {};
  }
  if (!legacy.token) return;

  const current = await api("/api/keys");
  if (!current.configured) {
    await api("/api/keys", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: legacy.token, marker: legacy.marker || "" }),
    });
    showNotice(
      "金鑰已搬到站台",
      "原本存在這台瀏覽器的查價金鑰,現在改存在站台上了,換裝置不用再填一次。",
      "info",
      "keys-migrated"
    );
  }
  localStorage.removeItem(LEGACY_KEY_STORE);
}

function wireKeyPanel() {
  $("#key-save").addEventListener("click", async () => {
    const token = $("#key-token").value.trim();
    const marker = $("#key-marker").value.trim();
    if (!token && !marker) return;
    try {
      await api("/api/keys", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, marker }),
      });
      await renderKeyState();
      await loadStatus();
      if (token) {
        $("#keys").open = false;
        showNotice("金鑰已儲存", "再按一次「組票」就會帶著它去查價。", "info", "keys-saved");
      }
    } catch (error) {
      $("#key-state").className = "keys__state keys__state--bad";
      $("#key-state").textContent = error.message;
    }
  });

  $("#key-clear").addEventListener("click", async () => {
    await api("/api/keys", { method: "DELETE" });
    await renderKeyState();
    await loadStatus();
  });
}

/** Basic auth has no "log out" — the browser keeps sending the old credentials
 *  until something rejects them. So the honest thing to say after a change is
 *  "you're about to be asked again", not "done". */
function wirePasswordPanel() {
  const state_ = $("#pw-state");

  $("#pw-save").addEventListener("click", async () => {
    const current = $("#pw-current").value;
    const next = $("#pw-new").value;
    state_.className = "keys__state";
    state_.textContent = "";

    if (!current || !next) {
      state_.className = "keys__state keys__state--bad";
      state_.textContent = "兩欄都要填。";
      return;
    }

    try {
      const body = await api("/api/password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ current, new: next }),
      });
      $("#pw-current").value = "";
      $("#pw-new").value = "";
      state_.className = "keys__state keys__state--ok";
      state_.textContent = body.note;
    } catch (error) {
      state_.className = "keys__state keys__state--bad";
      state_.textContent = error.message;
    }
  });
}

/* -------------------------------------------------------------- airlines */

/* 這個選單是**全球清單**,不是「這條航線有誰飛」。免費的航線資料表薄到不能當
 * 選單:台北出發的日本線裡還留著已經停業的復興(GE)與捷星亞洲(3K),卻沒有
 * 星宇(JX)跟台灣虎航(IT)—— 一個把 JX 藏起來、把 GE 端出來的選單,
 * 比沒有選單更糟。所以選單不受航線限制,而選的結果只跟著連結出去。 */

function airlineChip(airline) {
  const chip = el("button", "chip", airline.name);
  chip.type = "button";
  chip.title = airline.code;
  state.airlineNames.set(airline.code, airline.name);
  const paint = () =>
    chip.setAttribute("aria-pressed", String(state.airlines.has(airline.code)));
  chip.addEventListener("click", () => {
    if (state.airlines.has(airline.code)) state.airlines.delete(airline.code);
    else state.airlines.add(airline.code);
    paint();
    renderAirlineCount();
    // 已經有結果在畫面上時,連結是舊的 —— 講出來,不要讓使用者以為改完就生效了。
    noteStale();
  });
  paint();
  return chip;
}

function noteStale() {
  if ($("#reverse-results").hidden) return;
  showNotice(
    "篩選條件改了",
    "已經在畫面上的價格與連結還是舊的。再按一次「組票」就會套用。",
    "info",
    "filters-restale"
  );
}

function renderAirlineCount() {
  const picked = [...state.airlines];
  $("#airline-count").textContent = picked.length
    ? picked.map((code) => state.airlineNames.get(code) || code).join("、")
    : "不限";
  // 已選的也要在搜尋結果那排跟著亮起來,否則同一家會出現兩個不同狀態。
  for (const chip of document.querySelectorAll("#airline-chips .chip, #airline-found .chip")) {
    chip.setAttribute("aria-pressed", String(state.airlines.has(chip.title)));
  }
}

async function wireAirlinePanel() {
  let body;
  try {
    body = await api("/api/ref/airlines?limit=14");
  } catch {
    return;  // 純加值:清單拿不到,站台照常運作,只是不能挑航空公司
  }
  const chips = $("#airline-chips");
  chips.replaceChildren();
  for (const airline of body.airlines) chips.append(airlineChip(airline));

  let timer = null;
  $("#airline-search").addEventListener("input", (event) => {
    const q = event.target.value.trim();
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const found = $("#airline-found");
      if (!q) {
        found.replaceChildren();
        return;
      }
      try {
        const result = await api(`/api/ref/airlines?limit=12&q=${encodeURIComponent(q)}`);
        found.replaceChildren();
        if (!result.airlines.length) {
          found.append(el("span", "alts__label", `找不到「${q}」`));
          return;
        }
        for (const airline of result.airlines) found.append(airlineChip(airline));
      } catch {
        found.replaceChildren();
      }
    }, 250);
  });
  renderAirlineCount();
}

/* ==========================================================================
 * 倒買法:兩趟旅行交叉綁票
 *
 * 這個模式**不比價**,而那不是還沒做完 —— 台灣出發的航線沒有來回票的快取資料,
 * 而倒買法省的就是來回計價。用單程加總去猜,算出來的數字保證看不到那個效果。
 * 所以這裡產出的是組票與連結,價格交給訂票網站。有數字的只有單程票
 * (四段全拆的四張、單程＋反向的兩張);倒買票永遠不猜價。
 * ========================================================================== */

const reverseState = {
  home: { country: "TW", selected: new Set(["TPE", "TSA"]), label: "台北" },
  trips: [
    { country: "JP", selected: new Set(), label: "", depart: "", nights: 5, flex: true },
    { country: "JP", selected: new Set(), label: "", depart: "", nights: 5, flex: true },
  ],
};

/** 出發日 + 玩幾天,而不是兩個日期欄。
 *
 *  而且日期**刻意可以前後浮動**。上游快取是別人搜出來的,同一條航線某幾天有價
 *  某幾天沒有,而使用者無從得知是哪幾天 —— 實測同一組行程只把第二趟從 10/20
 *  挪到 10/06,算得出總價的機場組合就從 0 種變成 36 種。把「猜中有資料的那天」
 *  丟給使用者,等於保證他看到一片空白。 */
function tripDatesRow(trip, onChange) {
  const row = el("div", "stop__row");

  const departField = el("label", "field");
  departField.append(el("span", "field__label", "大概哪天出發"));
  const depart = el("input");
  depart.type = "date";
  depart.value = trip.depart;
  depart.addEventListener("change", () => { trip.depart = depart.value; onChange(); });
  departField.append(depart);

  const nightsField = el("label", "field field--narrow");
  nightsField.append(el("span", "field__label", "玩幾晚"));
  const nights = el("input");
  nights.type = "number";
  nights.min = "1";
  nights.max = "30";
  nights.value = trip.nights;
  nights.addEventListener("change", () => {
    trip.nights = Math.max(1, Number(nights.value) || 1);
    onChange();
  });
  nightsField.append(nights);

  const flex = el("label", "switch");
  const box = el("input");
  box.type = "checkbox";
  box.checked = trip.flex;
  box.addEventListener("change", () => { trip.flex = box.checked; onChange(); });
  flex.append(box, el("span", null, "前後幾天都可以"));

  row.append(departField, nightsField, flex);
  return row;
}

/** 使用者填的「大概這天」變成送給後端的區間。
 *  勾了浮動就前後各 4 天、天數 ±1;沒勾就是確切那天。 */
function tripWindow(trip) {
  const shift = (iso, days) => {
    const d = new Date(iso + "T00:00:00");
    d.setDate(d.getDate() + days);
    return d.toISOString().slice(0, 10);
  };
  const span = trip.flex ? 4 : 0;
  const wobble = trip.flex ? 1 : 0;
  return {
    codes: [...trip.selected],
    label: trip.label,
    depart_earliest: shift(trip.depart, -span),
    depart_latest: shift(trip.depart, span),
    nights_min: Math.max(1, trip.nights - wobble),
    nights_max: trip.nights + wobble,
  };
}

async function renderReversePlan() {
  const chain = $("#reverse-chain");
  chain.replaceChildren();

  const origin = el("div", "stop");
  origin.append(el("div", "stop__role", "出發"));
  const originBody = el("div", "stop__body");
  origin.append(originBody);
  chain.append(origin);
  await renderPlace(originBody, reverseState.home, renderReversePlan);

  for (const [index, trip] of reverseState.trips.entries()) {
    const node = el("div", "stop");
    node.append(el("div", "stop__role", `第 ${index + 1} 趟`));
    const body = el("div", "stop__body");
    node.append(body);
    chain.append(node);

    await renderPlace(body, trip, renderReversePlan);
    body.append(tripDatesRow(trip, renderReversePlan));
  }
}

/** 一張票畫成一條航線。兩張票上下並排,交叉的地方就看得見了。 */
function ticketRow(ticket, currency) {
  const row = el("div", "ticket");

  const role = el("div", "ticket__role");
  // 名詞只留「第幾張票」跟「從哪出發」。原本這裡同時掛著 A 票、包覆票、開口 ——
  // 三個標籤沒有一個講的是使用者要做什麼,而使用者回報看不懂。
  role.append(el("b", "ticket__code", ticket.role));
  if (ticket.code) role.append(el("div", "ticket__from", ticket.code));
  if (ticket.pricing) {
    role.append(el("div", "ticket__price",
      ticket.pricing.total != null ? money(ticket.pricing.total, currency) : "查無資料"));
  }
  if (ticket.note) role.append(el("div", "ticket__note", ticket.note));
  row.append(role);

  const body = el("div", "ticket__body");

  const line = el("div", "routeline");
  const labels = el("div", "legs");
  const stops = [];
  ticket.legs.forEach((leg, i) => {
    if (i > 0 && ticket.legs[i - 1].destination !== leg.origin) {
      // 票上的缺口。這裡**不**寫「自己走」—— 倒買法的缺口是另一張票飛掉的。
      stops.push({ code: ticket.legs[i - 1].destination, date: null, gapBefore: false });
      stops.push({ code: leg.origin, date: leg.date, gapBefore: true, tag: leg.code });
    } else if (i > 0) {
      stops.push({ code: leg.origin, date: leg.date, tag: leg.code });
    } else {
      stops.push({ code: leg.origin, date: leg.date, tag: leg.code });
    }
    if (i === ticket.legs.length - 1) {
      stops.push({ code: leg.destination, date: null });
    }
  });

  stops.forEach((stop, i) => {
    if (i > 0) {
      const seg = el("span", stop.gapBefore ? "seg seg--surface" : "seg seg--fly");
      seg.append(el("span", "seg__glyph", stop.gapBefore ? "另一張票" : "✈"));
      line.append(seg);
    }
    line.append(el("span", "node"));
    const label = el("div", "legs__stop");
    // 代號掛在那一段的**起點**上,因為 A1 指的是一段航程,不是一個機場。
    if (stop.tag) label.append(el("div", "legs__tag", stop.tag));
    label.append(el("div", "legs__code", stop.code));
    if (stop.date) label.append(el("div", "legs__date", stop.date.slice(5)));
    labels.append(label);
  });

  body.append(line, labels);

  // 「查無資料」單獨擺在那裡是條死路。這條航線通常還是有幾天有價的 ——
  // 抓價本來就是整月一起抓的,那些日子就在手上,講出來就變成下一步。
  for (const alt of ticket.pricing?.alternatives || []) {
    body.append(gapDetail(alt.gap, alt.alternatives, currency, alt.leg));
  }

  body.append(linkChips(ticket.links));
  // 只有整張票才要填 —— 單程票站台自己算得出來。
  if (!ticket.priceable && ticket.legs.length > 1) body.append(quoteRow(ticket));

  row.append(body);
  return row;
}

/** 三個出口,三種不同的東西 —— 而它們在畫面上長得一模一樣。
 *
 *  使用者回報過「航空公司沒篩選到」:當時只有 Google Flights 帶篩選,按下 Kayak
 *  或 Aviasales 看到的是全部航空公司,而按鈕上完全看不出差別。現在每顆按鈕直接
 *  標出它是什麼語言、有沒有照選的航空公司篩。
 *
 *  這些狀態全部來自後端的實測結果(`deeplinks.LINK_INFO`),不是前端猜的。 */
function linkChips(links) {
  const row = el("div", "ticket__links");
  const picked = state.airlines.size > 0;
  const nonstop = $("#nonstop").checked;
  for (const [name, href] of Object.entries(links)) {
    const info = state.linkInfo[name] || {};
    const link = el("a", "chip chip--link");
    link.append(el("span", null, info.label || name));

    const tags = [];
    if (info.locale) tags.push(info.locale);
    // 只在真的選了航空公司的時候才標篩選狀態 —— 沒選的時候「未篩選」
    // 是廢話,而廢話會把真正重要的那一個字淹掉。
    if (picked) tags.push(info.filters_airlines ? "航空公司已篩" : "航空公司未篩");
    if (nonstop) tags.push(info.filters_stops ? "直達" : "直達未篩");
    const missed = (picked && !info.filters_airlines) || (nonstop && !info.filters_stops);
    if (tags.length) {
      link.append(el("span", `chip__tag${missed ? " chip__tag--warn" : ""}`,
                     tags.join("・")));
    }

    link.href = href;
    link.target = "_blank";
    link.rel = "noopener";
    if (missed) {
      link.title = "這個網站的篩選參數我們沒有驗證過,寧可不帶 —— " +
                   "帶錯的話它會回一張空清單,而空清單長得就像「那天沒有班機」。";
    }
    row.append(link);
  }
  return row;
}

/** 怎麼飛。票是交叉的,行程不是 —— 每一趟照樣是去了再回來。
 *
 *  這是倒買法唯一真正難懂的地方。兩張交叉的票畫在那裡,不寫這段,使用者會以為
 *  自己得照票面順序飛(先台北→福岡,再沖繩→台北?),那個誤解會直接勸退他。 */
function sequenceBlock(plan) {
  if (!plan.sequence?.length) return null;
  const box = el("div", "sequence");
  box.append(el("div", "sequence__title", "怎麼飛"));
  for (const trip of plan.sequence) {
    const row = el("div", "sequence__trip");
    row.append(el("span", "sequence__when",
      `${trip.label} ${trip.depart.slice(5)}–${trip.back.slice(5)}`));
    row.append(el("span", "sequence__codes", trip.codes.join(" → ")));
    box.append(row);
  }
  return box;
}

/** 四段各自的單程價,當參考基準。
 *
 *  ⚠️ 逐段列、**不給總和**,而那不是偷懶:倒買法省錢的機制就是來回計價,
 *  單程加總必然看不到那個效果,而且錯的方向剛好會讓倒買法看起來更好。
 *  這裡要回答的是「市價大概這樣」,不是「這兩張票多少錢」。 */
function referenceBlock(legs, currency) {
  if (!legs?.length) return null;
  const box = el("div", "reference");
  box.append(
    el("div", "reference__title", "參考:同樣這四段,各自買單程要多少")
  );
  box.append(
    el("div", "reference__note",
       "這不是上面那兩張票的價格,也不能加起來當總價 —— " +
       "倒買法省的就是來回計價,單程加總看不到那個效果。點過去比比看那兩張票有沒有更便宜。")
  );
  for (const leg of legs) {
    const row = el("div", "reference__row");
    row.append(el("span", "reference__code", leg.code || ""));
    row.append(el("span", "reference__leg",
      `${leg.origin}→${leg.destination} ${leg.date.slice(5)}`));
    const money_ = el("span",
      leg.price != null ? "reference__price" : "reference__price reference__price--none",
      leg.price != null ? money(leg.price, currency) : "查無資料");
    row.append(money_);
    // 把「是誰飛的」直接掛在數字旁邊。使用者選了長榮卻看到別家的價,
    // 光靠一段說明是解釋不完的 —— 讓數字自己說它屬於誰。
    if (leg.airline) {
      const name = state.airlineNames.get(leg.airline) || leg.airline;
      const mine = !state.airlines.size || state.airlines.has(leg.airline);
      row.append(el("span", `reference__air${mine ? "" : " reference__air--other"}`, name));
    }
    box.append(row);
    if (leg.price == null) {
      box.append(gapDetail(leg.gap, leg.alternatives, currency,
                           `${leg.origin}→${leg.destination}`));
    }
  }
  return box;
}

/** 畫面上第一個數字:同樣這四段各自買單程要多少。
 *
 *  這是**唯一整組買法都算得出來的總價**,也是判斷反向票值不值得的基準線。
 *  刻意不寫成「上限」:實測 16 組來回票對兩張單程,有 4 組來回票**比較貴**
 *  (最高 1.117 倍,幾乎都是廉航 —— 廉航的來回票本來就是兩張單程黏起來)。
 *  標成上限而使用者點進去發現更貴,錯的方向剛好是最糟的那個。 */
/** 使用者選了航空公司之後,這個數字到底跟他有沒有關係。
 *
 *  最糟的情況不是「有幾段不符合」,是**一段都不符合**:實測選星宇 + 直飛,
 *  四段裡兩段是捷星、兩段連誰飛的都不知道,星宇在這四條航線的快取裡一次都沒出現過
 *  —— 而畫面照樣端出一個看起來很篤定的總價。設了兩個條件卻完全看不出設過,
 *  那不是資訊不足,是畫面在誤導。 */
function airlineVerdict(group, currency) {
  const legs = group.plans.find((p) => p.method === "reverse")?.reference_legs || [];
  const known = legs.map((l) => l.airline).filter(Boolean);
  const mine = known.filter((code) => state.airlines.has(code));
  const wanted = [...state.airlines]
    .map((code) => state.airlineNames.get(code) || code).join("、");
  const others = [...new Set(known.filter((code) => !state.airlines.has(code)))]
    .map((code) => state.airlineNames.get(code) || code);

  const box = el("div", "total__warn");
  if (mine.length) {
    box.append(el("div", null,
      `四段裡有 ${mine.length} 段是${wanted},其餘不是 —— 每段旁邊都標了是誰飛的。`));
    return box;
  }

  box.append(el("b", null, `這個價跟${wanted}無關。`));
  box.append(el("div", null,
    known.length
      ? `這四段目前拿得到的是${others.join("、")},${wanted}一段都沒有。`
      : `這四段目前連是誰飛的都拿不到,所以無法確認有沒有${wanted}。`));
  box.append(el("div", "total__why",
    `上游一天只給最便宜的那一筆票價,而最便宜的幾乎都是廉航 —— ` +
    `${wanted}要看真價,只能點下面的連結(連結有幫你篩)。`));
  return box;
}

function totalCard(group, currency) {
  const card = el("div", "total");
  if (group.split_total != null) {
    card.append(el("div", "total__label", "同樣這四段,各自買單程"));
    card.append(el("div", "total__price", money(group.split_total, currency)));

    // 篩選條件的狀態要**貼在數字旁邊**,不能只寫在收起來的面板裡 ——
    // 那個面板預設是關的,使用者根本沒讀到,然後點進 Google Flights 發現
    // 價格對不上。他回報的就是這件事。
    if ($("#nonstop").checked) {
      card.append(el("div", "total__applied", "只要直達 —— 已算進這個數字"));
    }
    if (state.airlines.size) card.append(airlineVerdict(group, currency));

    card.append(el("div", "total__note",
      "反向票要比這個便宜才值得買。"));
  } else {
    // 距今幾個月。超出視野的話,「缺 TPE→ITM…」這種列表會被讀成「壞了」,
    // 但真正的訊息是「太遠了,而且站台這部分本來就幫不上忙 —— 其他部分還能用」。
    const first = group.plans[0]?.sequence?.[0]?.depart;
    const monthsOut = first
      ? Math.round((new Date(first) - new Date()) / 86400000 / 30)
      : null;

    if (monthsOut != null && monthsOut >= 4) {
      card.append(el("div", "total__label", "這組日期太遠,站台查不到價"));
      card.append(el("div", "total__price total__price--none",
        `出發還有大約 ${monthsOut} 個月`));
      card.append(el("div", "total__note",
        "這個資料源的快取是別人搜出來的,大約只有三個月的視野 —— 再遠就一列都沒有," +
        "不是沒有班機。"));
      card.append(el("div", "total__applied",
        "下面的組票、日期、連結、比一比照常可以用:點連結查到真價,填回來就有答案。"));
    } else {
      card.append(el("div", "total__label", "算不出總價"));
      card.append(el("div", "total__price total__price--none",
        group.missing.length ? `缺 ${group.missing.join("、")}` : "缺資料"));
      card.append(el("div", "total__note",
        "把行程往前挪,或勾「前後幾天都可以」,通常就有價格了。"));
    }
  }
  return card;
}

/* ---- 把查到的真價填回來 ------------------------------------------------
 *
 * 這個站算得出「四段全拆單程」的總價,但算不出那兩張反向票 —— 上游一天只存
 * 一筆最便宜的票價,不分航空公司,所以星宇之類的傳統航空永遠不會出現
 * (實測:TPE→NRT 快取裡有 13 家,沒有一家是 JX)。
 *
 * 但使用者點連結過去兩下就查得到。站台真正做得到、而手算很煩的是**比較**,
 * 所以把數字接回來由站台算。存進 localStorage:使用者會離開頁面去查,
 * 回來時如果要重按一次「組票」才看得到自己剛填的東西,這個流程就沒人會用。 */

const QUOTE_STORE = "air.quotes";

function ticketSignature(ticket) {
  return ticket.legs.map((l) => `${l.origin}${l.destination}${l.date}`).join("|");
}

function loadQuotes() {
  try { return JSON.parse(localStorage.getItem(QUOTE_STORE) || "{}"); }
  catch { return {}; }
}

function saveQuote(sig, value) {
  const all = loadQuotes();
  if (value == null) delete all[sig];
  else all[sig] = value;
  localStorage.setItem(QUOTE_STORE, JSON.stringify(all));
}

/* 幣別是必要的,不是選配。反向機票省錢的那張票從外站出發,**就用外站的幣別賣**
 * —— 使用者實測的星宇例子:台北出發那張 TWD 17,095,大阪出發那張 JPY 84,540。
 * 只吃 TWD 的欄位對這個功能來說等於壞的。 */
const QUOTE_CURRENCIES = ["TWD", "JPY", "KRW", "USD", "HKD", "EUR", "CNY", "THB", "SGD"];

function quoteRow(ticket, label) {
  const sig = ticketSignature(ticket);
  const row = el("div", "quote");
  row.append(el("span", "quote__label", label || "查到多少?填回來"));

  const saved = loadQuotes()[sig] || {};
  const input = el("input", "quote__input");
  input.type = "number";
  input.min = "0";
  input.step = "1";
  input.placeholder = "例如 17095";
  if (saved.amount != null) input.value = saved.amount;

  const currency = el("select", "quote__currency");
  for (const code of QUOTE_CURRENCIES) {
    const option = el("option", null, code);
    option.value = code;
    if (code === (saved.currency || "TWD")) option.selected = true;
    currency.append(option);
  }

  const commit = () => {
    const amount = input.value.trim() === "" ? null : Number(input.value);
    saveQuote(sig, Number.isFinite(amount) && amount != null
      ? { amount, currency: currency.value } : null);
    redrawCompare();
  };
  input.addEventListener("input", commit);
  currency.addEventListener("change", commit);

  row.append(input, currency);
  return row;
}

/** 換成站台幣別。拿不到匯率就回 null —— 用一個編的匯率算出來的比較,
 *  比不比較更糟。 */
function toBase(quote) {
  if (!quote || quote.amount == null) return null;
  if (quote.currency === state.fxBase) return quote.amount;
  const rate = state.fx[quote.currency];
  return rate ? quote.amount / rate : null;
}

/** 比一比:兩張反向票 vs 四段全拆單程。
 *
 *  這是這個站唯一能做、而使用者手上做不到的事 —— 他有兩個數字,但要跟
 *  「同樣四段各自買單程」比,得先知道那個總價是多少,而那正是站台算出來的。 */
function compareCard(group, currency) {
  const card = el("div", "compare");
  card.id = "compare";
  card.append(el("div", "compare__title", "比一比"));

  const reverseTickets = group.plans.find((p) => p.method === "reverse")?.tickets || [];
  const normalTickets = group.plans.find((p) => p.method === "normal")?.tickets || [];
  const quotes = loadQuotes();
  const sum = (tickets) => {
    const values = tickets.map((t) => toBase(quotes[ticketSignature(t)]));
    return values.every((v) => v != null)
      ? values.reduce((a, b) => a + b, 0)
      : null;
  };

  // 普通買法(兩張各自的來回票)才是使用者真正要比的基準 —— 那是「不用這招的話
  // 我本來會付多少」。四段全拆單程是站台自己算得出來的另一個參考點。
  card.append(el("div", "compare__group", "普通買法:兩張各自的來回票"));
  for (const [index, ticket] of normalTickets.entries()) {
    card.append(quoteRow(ticket, `第 ${index + 1} 趟來回`));
  }

  const reverseTotal = sum(reverseTickets);
  const normalTotal = sum(normalTickets);
  const split = group.split_total;

  const rows = [];
  if (normalTotal != null) rows.push(["普通買法:兩張來回票", normalTotal]);
  if (reverseTotal != null) rows.push(["反向機票:你查到的兩張", reverseTotal]);
  if (split != null) rows.push(["四段全拆:四張單程(站台算的)", split]);

  if (reverseTotal == null) {
    card.append(el("div", "compare__hint",
      "把上面兩張反向票查到的價填回來,就會出現比較。外站出發那張多半是外幣 —— " +
      "記得把幣別一起改,站台會換算。"));
    return card;
  }

  const best = Math.min(...rows.map(([, v]) => v));
  for (const [label, value] of rows) {
    const row = el("div", `compare__row${value === best ? " compare__row--best" : ""}`);
    row.append(el("span", null, label));
    row.append(el("span", "compare__price", money(Math.round(value), currency)));
    card.append(row);
  }

  if (normalTotal != null) {
    const delta = normalTotal - reverseTotal;
    const pct = Math.abs(delta) / normalTotal * 100;
    card.append(
      el("div", delta > 0 ? "compare__verdict" : "compare__verdict compare__verdict--worse",
         delta > 0
           ? `反向機票省 ${money(Math.round(delta), currency)}(${pct.toFixed(1)}%)`
           : delta < 0
             ? `反向機票貴 ${money(Math.round(-delta), currency)} —— 這組行程照普通買法買就好。`
             : "兩種一樣價。")
    );
    if (delta > 0) {
      card.append(el("div", "compare__hint",
        "省下來的是用「兩趟都一定會去」換的:同一張票必須依序使用,第一趟沒搭,第二趟就失效。"));
    }
  } else {
    card.append(el("div", "compare__hint",
      "再把「普通買法」那兩張來回票的價填上去,就知道這招對你這組行程省多少。"));
  }
  return card;
}

function redrawCompare() {
  const old = document.querySelector("#compare");
  if (!old || !state.lastGroup) return;
  old.replaceWith(compareCard(state.lastGroup, state.lastCurrency));
}

/** 讓使用者自己挑日期組合。
 *
 *  後端本來就排了 12 種(機場 × 日期)的組合、照總價排序,但畫面只顯示第一種 ——
 *  等於幫使用者決定了日期,而他要的是「有哪幾天可以選」。
 *
 *  ⚠️ 挑不掉的是航空公司:上游一天只給**最便宜的那一筆**票價(五種參數寫法都試過,
 *  17 列永遠對 17 天,一天一筆),而最便宜的幾乎都是廉航 —— TPE→NRT 九月回來的是
 *  TR/MM/GK/ZE/LJ,一天都沒有長榮。所以這裡列得出「哪幾天有票、誰飛的」,
 *  列不出「長榮有票的日子」。不符合你選的航空公司的,標出來但不藏起來 ——
 *  藏起來只會得到一張空清單。 */
function datePicker(body, current, onPick) {
  const box = el("div", "picker");
  box.append(el("div", "picker__title", `其他日期組合(共 ${body.groups.length} 種,照總價排)`));

  for (const [index, group] of body.groups.entries()) {
    const seq = group.plans[0]?.sequence || [];
    const row = el("button", `picker__row${index === current ? " picker__row--on" : ""}`);
    row.type = "button";
    row.append(el("span", "picker__when",
      seq.map((t) => `${t.label} ${t.depart.slice(5)}–${t.back.slice(5)}`).join("  ")));

    const legs = group.plans.find((p) => p.method === "reverse")?.reference_legs || [];
    const known = legs.map((l) => l.airline).filter(Boolean);
    if (known.length) {
      const names = [...new Set(known)]
        .map((code) => state.airlineNames.get(code) || code);
      const off = state.airlines.size && !known.some((c) => state.airlines.has(c));
      row.append(el("span", `picker__air${off ? " picker__air--other" : ""}`, names.join("・")));
    }

    row.append(el("span",
      group.split_total != null ? "picker__price" : "picker__price picker__price--none",
      group.split_total != null ? money(group.split_total, body.currency) : "缺價"));
    row.addEventListener("click", () => onPick(index));
    box.append(row);
  }
  return box;
}

function methodCard(plan, currency) {
  const card = el("div", `method${plan.method === "reverse" ? " method--reverse" : ""}`);

  const head = el("div", "method__head");
  head.append(el("div", "method__name", plan.method_label));

  if (plan.pricing?.total != null) {
    const price = el("div", "method__price", money(plan.pricing.total, currency));
    head.append(price);
  } else if (plan.pricing) {
    head.append(
      el("div", "method__noprice",
         `有 ${plan.pricing.missing.join("、")} 查無資料,所以不算總價`)
    );
  } else {
    head.append(el("div", "method__noprice", plan.unavailable_reason));
  }
  card.append(head);

  for (const ticket of plan.tickets) card.append(ticketRow(ticket, currency));

  const order = sequenceBlock(plan);
  if (order) card.append(order);

  if (plan.risks?.length) {
    const risks = el("ul", "card__risks");
    risks.style.marginTop = "0.8rem";
    for (const risk of plan.risks) risks.append(el("li", null, risk));
    card.append(risks);
  }

  const reference = referenceBlock(plan.reference_legs, currency);
  if (reference) card.append(reference);
  return card;
}

async function runReverse(event) {
  event.preventDefault();
  const button = $("#rev-submit");
  $("#notices").replaceChildren();
  shownNotices.clear();

  const [first, second] = reverseState.trips;
  if (!reverseState.home.selected.size || !first.selected.size || !second.selected.size) {
    showNotice("還有地方沒選", "出發地和兩趟的目的地都要至少選一個機場。", "alert");
    return;
  }
  if (!first.depart || !second.depart) {
    showNotice("日期沒填完", "兩趟旅行都要給一個大概的出發日。", "alert");
    return;
  }

  button.disabled = true;
  button.textContent = "組票中…";
  try {
    const body = await api("/api/reverse", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        home: [...reverseState.home.selected],
        first: tripWindow(first),
        second: tripWindow(second),
        try_both_orders: false,
        passengers: Number($("#rev-passengers").value) || 1,
        cabin: $("#rev-cabin").value,
        airlines: [...state.airlines],
        nonstop: $("#nonstop").checked,
      }),
    });

    state.linkInfo = body.link_info || {};

    for (const warning of body.warnings || []) {
      showNotice("注意", warning, "alert", topicOf(warning));
    }

    const list = $("#rev-list");
    // 摘要要跟著使用者挑的那一組走,所以跟卡片一起重畫 —— 放在 draw 外面
    // 只會永遠描述第 0 組,而且那正是上一版把 `group` 用在作用域外、
    // 讓整個請求丟出 ReferenceError 的地方。
    const draw = (index) => {
      const group = body.groups[index];
      const chosen = group.plans.filter((plan) => plan.method === "reverse");
      list.replaceChildren();
      state.lastGroup = group;
      state.lastCurrency = body.currency;
      list.append(totalCard(group, body.currency));
      list.append(datePicker(body, index, draw));
      for (const plan of chosen) list.append(methodCard(plan, body.currency));
      list.append(compareCard(group, body.currency));

      const seq = group.plans[0]?.sequence || [];
      $("#rev-summary").textContent = seq.length
        ? seq.map((t) => `${t.label} ${t.depart.slice(5)}–${t.back.slice(5)}`).join(" · ") +
          ` — ${body.group_count} 種日期組合裡的第 ${index + 1} 種`
        : `${body.route_pairs} 條航線 · ${body.months.join("、")}`;
    };
    draw(0);

    $("#reverse-results").hidden = false;
    $("#reverse-results").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    showNotice("組票沒有成功", error.message, "alert");
  } finally {
    button.disabled = false;
    button.textContent = "組票";
  }
}

async function initReverse() {
  // 預設刻意落在**有資料的視窗內**。上游快取只有大約三個月的視野(實測:
  // 今天 8/09,TPE→NRT 在 9 月有 29 天有價、12 月 0 天),而舊的預設是
  // +60 天與 +122 天 —— 第二趟正好在死區,所以第一屏永遠是空的。
  const iso = (d) => d.toISOString().slice(0, 10);
  const at = (days) => {
    const d = new Date();
    d.setDate(d.getDate() + days);
    return iso(d);
  };
  const [first, second] = reverseState.trips;
  first.depart = at(30);
  second.depart = at(75);

  const jp = await airportsFor("JP");
  const tokyo = jp.find((c) => c.code === "TYO");
  const osaka = jp.find((c) => c.code === "OSA");
  if (tokyo) { tokyo.airports.forEach((x) => first.selected.add(x.code)); first.label = tokyo.name; }
  if (osaka) { osaka.airports.forEach((x) => second.selected.add(x.code)); second.label = osaka.name; }

  await renderReversePlan();
  $("#reverse-form").addEventListener("submit", runReverse);
}

async function init() {
  const body = await api("/api/ref/countries");
  state.countries = body.countries;

  wireKeyPanel();
  wirePasswordPanel();
  wireAirlinePanel();
  $("#nonstop").addEventListener("change", noteStale);
  api("/api/fx")
    .then((body) => { state.fx = body.rates || {}; state.fxBase = body.base || "TWD"; })
    .catch(() => {});   // 拿不到就只能比同幣別的,不擋其他功能
  await migrateLegacyKeys();
  await renderKeyState();
  await initReverse();
  loadStatus();
}

init().catch((error) => showNotice("啟動失敗", error.message, "alert"));
