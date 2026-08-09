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

function countrySelect(value, onChange) {
  const select = el("select");
  for (const country of state.countries) {
    const option = el("option", null, `${country.name_zh} (${country.airport_count})`);
    option.value = country.code;
    if (country.code === value) option.selected = true;
    select.append(option);
  }
  select.addEventListener("change", () => onChange(select.value));
  return select;
}

/** A city and its airports. Tapping the city takes all of them; tapping a
 *  code takes just that one. Multi-airport cities are where substitution
 *  actually saves money, so both levels have to be reachable. */
function cityGroup(city, selected, onToggle) {
  const group = el("div", "citygroup");
  const codes = city.airports.map((a) => a.code);
  const all = codes.every((code) => selected.has(code));

  const name = el("button", "citygroup__name", city.name);
  name.type = "button";
  name.setAttribute("aria-pressed", String(all));
  name.addEventListener("click", () => onToggle(codes, !all));
  group.append(name);

  for (const airport of city.airports) {
    const button = el("button", "citygroup__code", airport.code);
    button.type = "button";
    button.title = airport.name;
    button.setAttribute("aria-pressed", String(selected.has(airport.code)));
    button.addEventListener("click", () =>
      onToggle([airport.code], !selected.has(airport.code))
    );
    group.append(button);
  }
  return group;
}

async function renderPlace(container, place, onChange) {
  container.replaceChildren();

  const row = el("div", "stop__row");
  row.append(countrySelect(place.country, async (code) => {
    place.country = code;
    place.selected = new Set();
    await onChange();
  }));
  container.append(row);

  const cities = await airportsFor(place.country);
  const picker = el("div", "stop__row");
  // 只列前 12 個城市:超過這個數量的清單沒有人會讀,而機場上限本來就是 6。
  for (const city of cities.slice(0, 12)) {
    picker.append(
      cityGroup(city, place.selected, (codes, on) => {
        for (const code of codes) {
          if (on) place.selected.add(code);
          else place.selected.delete(code);
        }
        place.label = cities.find((c) =>
          c.airports.some((a) => place.selected.has(a.code))
        )?.name || place.label;
        onChange();
      })
    );
  }
  container.append(picker);
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
    { country: "JP", selected: new Set(), label: "", depart: "", back: "" },
    { country: "JP", selected: new Set(), label: "", depart: "", back: "" },
  ],
};

function tripDatesRow(trip, onChange) {
  const row = el("div", "stop__row");
  for (const [key, label] of [["depart", "出發"], ["back", "回程"]]) {
    const field = el("label", "field");
    field.append(el("span", "field__label", label));
    const input = el("input");
    input.type = "date";
    input.value = trip[key];
    input.addEventListener("change", () => {
      trip[key] = input.value;
      onChange();
    });
    field.append(input);
    row.append(field);
  }
  return row;
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
  // 「A 票 / B 票」是流通版本的講法,「包覆票 / 倒買票」說的是它為什麼長這樣。
  // 兩個都留:前者讓人對得上別人講的,後者讓人知道哪一張是省錢的那張。
  if (ticket.code) role.append(el("b", "ticket__code", `${ticket.code} 票`));
  role.append(el("b", null, ticket.role));
  if (ticket.open_jaw) role.append(document.createTextNode("開口"));
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
    row.append(
      el("span", leg.price != null ? "reference__price" : "reference__price reference__price--none",
         leg.price != null ? money(leg.price, currency) : "查無資料")
    );
    box.append(row);
    if (leg.price == null) {
      box.append(gapDetail(leg.gap, leg.alternatives, currency,
                           `${leg.origin}→${leg.destination}`));
    }
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
  if (!first.depart || !first.back || !second.depart || !second.back) {
    showNotice("日期沒填完", "兩趟旅行的去程與回程日期都要填。", "alert");
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
        first: { codes: [...first.selected], depart: first.depart, back: first.back,
                 label: first.label },
        second: { codes: [...second.selected], depart: second.depart, back: second.back,
                  label: second.label },
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

    // 只顯示「反向機票:兩張交叉的來回票」—— 流通版本講的就是這一種。
    // 其餘三種後端照樣算、測試照樣守著,想拿回來比較時只要改這一行。
    const plans = body.groups[0].plans.filter((plan) => plan.method === "reverse");

    const list = $("#rev-list");
    list.replaceChildren();
    for (const plan of plans) list.append(methodCard(plan, body.currency));

    $("#rev-summary").textContent =
      `${body.route_pairs} 條航線 · ${body.months.join("、")} · ` +
      `這兩張票都按來回計價,而台灣出發的航線沒有來回快取資料,所以站內不比價 —— ` +
      `價格請用每張票的連結各自查,下面的單程價只是參考基準。`;
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
  const start = new Date();
  start.setDate(start.getDate() + 60);
  const iso = (d) => d.toISOString().slice(0, 10);
  const [first, second] = reverseState.trips;

  const a = new Date(start), aBack = new Date(start);
  aBack.setDate(aBack.getDate() + 5);
  const b = new Date(start), bBack = new Date(start);
  b.setDate(b.getDate() + 62);
  bBack.setDate(bBack.getDate() + 67);
  first.depart = iso(a); first.back = iso(aBack);
  second.depart = iso(b); second.back = iso(bBack);

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
  await migrateLegacyKeys();
  await renderKeyState();
  await initReverse();
  loadStatus();
}

init().catch((error) => showNotice("啟動失敗", error.message, "alert"));
