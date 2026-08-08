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
  home: { country: "TW", selected: new Set(["TPE", "TSA"]), label: "台北" },
  stops: [],
  lastSearch: null,
  // 沒有即時報價來源時,「查即時價」按鈕只會回一句「未接即時報價來源」——
  // 白費一次點擊,而且旁邊的連結早就在做同一件事。
  hasLivePricing: false,
};

const $ = (selector) => document.querySelector(selector);

/* ---------------------------------------------------------------- fetch */

/* 金鑰只存在這台瀏覽器,查價時當成標頭送出。站台是公開的、沒有登入,
 * 存在伺服器上的金鑰等於任何找到設定面板的人都讀得走。 */
const KEY_STORE = "air.travelpayouts";

function loadKeys() {
  try {
    return JSON.parse(localStorage.getItem(KEY_STORE) || "{}");
  } catch {
    return {};
  }
}

function saveKeys(keys) {
  localStorage.setItem(KEY_STORE, JSON.stringify(keys));
}

function authHeaders() {
  const { token, marker } = loadKeys();
  const headers = {};
  if (token) headers["X-Travelpayouts-Token"] = token;
  if (marker) headers["X-Travelpayouts-Marker"] = marker;
  return headers;
}

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

function nightsRow(stop, onChange) {
  const row = el("div", "stop__row");
  row.append(el("span", "hop__label", "停留"));

  const min = el("input");
  min.type = "number";
  min.min = "0";
  min.max = "21";
  min.value = stop.nights_min;
  min.style.width = "4rem";
  min.addEventListener("change", () => {
    stop.nights_min = Math.max(0, Number(min.value));
    if (stop.nights_max < stop.nights_min) {
      stop.nights_max = stop.nights_min;
    }
    onChange();
  });

  const max = el("input");
  max.type = "number";
  max.min = "0";
  max.max = "21";
  max.value = stop.nights_max;
  max.style.width = "4rem";
  max.addEventListener("change", () => {
    stop.nights_max = Math.max(stop.nights_min, Number(max.value));
    onChange();
  });

  row.append(min, el("span", null, "到"), max, el("span", null, "晚"));
  return row;
}

function hopRow(index, onChange) {
  const hop = el("div", "hop");
  hop.append(el("span", "hop__label", "去下一站"));

  for (const [value, text] of [["surface", "自己走"], ["fly", "搭飛機"]]) {
    const button = el("button", "chip", text);
    button.type = "button";
    button.setAttribute("aria-pressed", String(state.stops[index].hop === value));
    button.addEventListener("click", () => {
      state.stops[index].hop = value;
      onChange();
    });
    hop.append(button);
  }
  return hop;
}

async function renderPlan() {
  const chain = $("#stopchain");
  chain.replaceChildren();

  const origin = el("div", "stop");
  origin.append(el("div", "stop__role", "出發"));
  const originBody = el("div", "stop__body");
  origin.append(originBody);
  chain.append(origin);
  await renderPlace(originBody, state.home, renderPlan);

  for (const [index, stop] of state.stops.entries()) {
    const node = el("div", "stop");
    node.append(el("div", "stop__role", `停留 ${index + 1}`));
    const body = el("div", "stop__body");
    node.append(body);
    chain.append(node);

    await renderPlace(body, stop, renderPlan);
    body.append(nightsRow(stop, renderPlan));

    if (state.stops.length > 1) {
      const remove = el("button", "stop__remove", "移除這站");
      remove.type = "button";
      remove.addEventListener("click", () => {
        state.stops.splice(index, 1);
        renderPlan();
      });
      body.querySelector(".stop__row").append(remove);
    }

    if (index < state.stops.length - 1) chain.append(hopRow(index, renderPlan));
  }

  const home = el("div", "stop");
  home.append(el("div", "stop__role", "回到"));
  home.append(el("div", "stop__body", state.home.label));
  chain.append(home);

  updateCostHint();
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

function freshness(hours) {
  if (hours == null) return "";
  if (hours < 1) return "剛剛抓的";
  if (hours < 24) return `${Math.round(hours)} 小時前`;
  return `${Math.round(hours / 24)} 天前`;
}

/** The signature element: solid where you fly, dashed where you don't. */
function routeLine(combo) {
  const wrap = el("div", "card__route");
  const line = el("div", "routeline");
  const labels = el("div", "legs");

  const stops = [];
  combo.legs.forEach((leg, index) => {
    if (index > 0) {
      const previous = combo.legs[index - 1];
      // A gap between two legs means the traveller got themselves there.
      if (previous.destination !== leg.origin) {
        stops.push({ code: previous.destination, place: previous.to_label, date: null });
        stops.push({ code: leg.origin, place: leg.from_label, date: leg.date, surfaceBefore: true });
      } else {
        stops.push({ code: leg.origin, place: leg.from_label, date: leg.date });
      }
    } else {
      stops.push({ code: leg.origin, place: leg.from_label, date: leg.date });
    }
    if (index === combo.legs.length - 1) {
      stops.push({ code: leg.destination, place: leg.to_label, date: null });
    }
  });

  stops.forEach((stop, index) => {
    if (index > 0) {
      const seg = el("span", stop.surfaceBefore ? "seg seg--surface" : "seg seg--fly");
      seg.append(el("span", "seg__glyph", stop.surfaceBefore ? "陸路" : "✈"));
      line.append(seg);
    }
    line.append(el("span", "node"));

    const label = el("div", "legs__stop");
    label.append(el("div", "legs__code", stop.code));
    label.append(el("div", "legs__place", stop.place));
    if (stop.date) label.append(el("div", "legs__date", stop.date.slice(5)));
    labels.append(label);
  });

  wrap.append(line, labels);
  return wrap;
}

function priceBlock(combo, currency) {
  const box = el("div");
  const total = money(combo.split_total, currency);

  if (total) {
    const price = el("div", "card__price", total);
    box.append(price);
    const stamp = el("span", "stamp", `快取・${freshness(oldestAge(combo))}`);
    box.append(stamp);
  } else {
    // 沒有價格的地方一定要說原因,否則會被讀成「這個組合很貴」。
    const missing = combo.legs.filter((leg) => leg.status !== "ok");
    const asked = missing.some((leg) => leg.status === "no_data");
    box.append(
      el(
        "div",
        "card__price card__price--none",
        asked ? "此航段查無資料" : "尚未查價"
      )
    );
    box.append(
      el(
        "span",
        "stamp stamp--none",
        missing.map((leg) => `${leg.origin}→${leg.destination}`).join(" / ")
      )
    );
  }

  // 比基準貴也要講。只在省錢時才顯示,等於把不利的比較悄悄藏起來。
  const delta = combo.savings_vs_baseline;
  if (delta != null && Math.round(delta) !== 0) {
    box.append(
      el(
        "div",
        delta > 0 ? "card__saving" : "card__saving card__saving--worse",
        delta > 0
          ? `比單城來回省 ${money(delta, currency)}`
          : `比單城來回貴 ${money(-delta, currency)}`
      )
    );
  }
  return box;
}

function oldestAge(combo) {
  const ages = combo.legs.map((leg) => leg.age_hours).filter((age) => age != null);
  return ages.length ? Math.max(...ages) : null;
}

function linkRow(label, links, extra) {
  const row = el("div", "card__actions");
  row.append(el("span", "label", label));
  for (const [name, href] of Object.entries(links)) {
    if (!href.startsWith("http")) continue;
    const link = el("a", "chip", { google_flights: "Google Flights", kayak: "Kayak", aviasales: "Aviasales" }[name] || name);
    link.href = href;
    link.target = "_blank";
    link.rel = "noopener";
    row.append(link);
  }
  if (extra) row.append(extra);
  return row;
}

function resultCard(combo, index, currency, options = {}) {
  const card = el("li", `card${index === 0 && !options.muted ? " card--best" : ""}`);

  const top = el("div", "card__top");
  top.append(el("div", "card__rank", options.muted ? combo.shape : `#${index + 1} ${combo.shape}`));
  top.append(priceBlock(combo, currency));
  card.append(top);

  card.append(routeLine(combo));

  if (combo.risks?.length) {
    const risks = el("ul", "card__risks");
    for (const risk of combo.risks) risks.append(el("li", null, risk));
    card.append(risks);
  }

  card.append(
    linkRow(
      "整趟一張票",
      combo.links.single_ticket,
      state.hasLivePricing ? verifyButton(combo, currency) : null
    )
  );

  const splitRow = el("div", "card__actions");
  splitRow.append(el("span", "label", "分段拼票"));
  for (const leg of combo.links.split) {
    const link = el("a", "chip", `${leg.leg} ${leg.date.slice(5)}`);
    link.href = leg.google_flights;
    link.target = "_blank";
    link.rel = "noopener";
    splitRow.append(link);
  }
  card.append(splitRow);

  return card;
}

/** 即時實價:同一個行程,拼票買法與一張票買法並排。 */
function verifyButton(combo, currency) {
  const button = el("button", "chip", "查即時價");
  button.type = "button";
  button.addEventListener("click", async () => {
    button.disabled = true;
    button.textContent = "查詢中…";
    try {
      const body = await api("/api/verify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          legs: combo.legs.map((leg) => ({
            origin: leg.origin,
            destination: leg.destination,
            date: leg.date,
          })),
          passengers: Number($("#passengers").value) || 1,
          cabin: $("#cabin").value,
        }),
      });
      button.replaceWith(verdictBox(body));
    } catch (error) {
      button.disabled = false;
      button.textContent = "查即時價";
      showNotice("即時查價沒有成功", error.message, "alert");
    }
  });
  return button;
}

function verdictBox(body) {
  const box = el("div", "verdict");
  const single = body.single_ticket;
  const split = body.split_tickets;

  const cheaperIsSplit =
    single.total != null && split.total != null ? split.total < single.total : null;

  for (const [label, quote, wins] of [
    ["一張票", single, cheaperIsSplit === false],
    ["分段拼票", split, cheaperIsSplit === true],
  ]) {
    const row = el("div", "verdict__row");
    row.append(el("span", null, label));
    row.append(
      el(
        "span",
        wins ? "verdict__win" : null,
        quote.total != null ? money(quote.total, quote.currency) : quote.unavailable_reason
      )
    );
    box.append(row);
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

/* ---------------------------------------------------------------- search */

function payload() {
  return {
    home: [...state.home.selected],
    stops: state.stops.map((stop) => ({
      codes: [...stop.selected],
      nights_min: stop.nights_min,
      nights_max: stop.nights_max,
      label: stop.label,
    })),
    depart_earliest: $("#depart-earliest").value,
    depart_latest: $("#depart-latest").value,
    try_both_orders: $("#both-orders").checked,
    internal_links:
      state.stops.length > 1 ? state.stops.slice(0, -1).map((s) => s.hop) : null,
    passengers: Number($("#passengers").value) || 1,
    cabin: $("#cabin").value,
  };
}

function updateCostHint() {
  const airports =
    state.home.selected.size +
    state.stops.reduce((sum, stop) => sum + stop.selected.size, 0);
  $("#cost-hint").textContent = airports
    ? `已選 ${airports} 個機場`
    : "還沒選機場";
}

function renderList(sectionId, listId, combos, currency, options) {
  const section = $(sectionId);
  const list = $(listId);
  list.replaceChildren();
  if (!combos.length) {
    section.hidden = true;
    return;
  }
  combos.forEach((combo, index) => list.append(resultCard(combo, index, currency, options)));
  section.hidden = false;
}

async function runSearch(event) {
  event.preventDefault();
  const button = $("#submit");
  $("#notices").replaceChildren();
  shownNotices.clear();

  if (!state.home.selected.size || state.stops.some((s) => !s.selected.size)) {
    showNotice("還有地方沒選", "每一站都要至少選一個機場,才知道要查哪條航線。", "alert");
    return;
  }

  button.disabled = true;
  const body = payload();

  try {
    button.textContent = "抓價中…";
    const warmed = await api("/api/search/warm", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(body),
    });
    for (const warning of warmed.warnings || []) {
      showNotice("沒有價格資料", warning, "alert", topicOf(warning));
    }

    button.textContent = "排名中…";
    const result = await api("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(body),
    });
    state.lastSearch = result;

    for (const warning of result.warnings || []) {
      showNotice("注意", warning, "alert", topicOf(warning));
    }

    const { counts, currency } = result;
    // 「顯示最便宜的 0」讀起來像壞掉了。沒有價格時要說清楚是沒有價格。
    $("#results-summary").textContent = counts.shown
      ? `${counts.combinations.toLocaleString()} 種組合 · ` +
        `${result.cost.api_calls} 次查價 · 顯示最便宜的 ${counts.shown}`
      : `${counts.combinations.toLocaleString()} 種組合,但目前沒有價格可以排名。` +
        `下面列出組合本身,票價請用每列的連結查。`;
    $("#gaps-summary").textContent =
      `共 ${counts.unpriceable.toLocaleString()} 種組合缺價格,以下是其中幾種。` +
      `缺資料不代表不便宜 —— 只代表沒有人搜過這條航線。`;

    renderList("#results", "#result-list", result.results, currency, {});
    if (!result.results.length && result.unpriceable.length) {
      // 有組合、只是沒有價格 —— 讓標題與說明留在畫面上,否則使用者只會看到
      // 一個叫「查無資料」的區塊,像是什麼都沒找到。
      $("#results").hidden = false;
    }
    renderList("#baselines", "#baseline-list", result.baselines, currency, { muted: true });
    renderList("#gaps", "#gap-list", result.unpriceable, currency, { muted: true });

    if (!result.results.length && !result.unpriceable.length) {
      showNotice("沒有可行的組合", "試著放寬日期區間,或多選幾個機場。", "info");
    }
    $("#results").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    showNotice("搜尋沒有成功", error.message, "alert");
  } finally {
    button.disabled = false;
    button.textContent = "找組合";
  }
}

/* ------------------------------------------------------------------ init */

function defaultDates() {
  const start = new Date();
  start.setDate(start.getDate() + 60);
  const end = new Date(start);
  end.setDate(end.getDate() + 6);
  const iso = (date) => date.toISOString().slice(0, 10);
  $("#depart-earliest").value = iso(start);
  $("#depart-latest").value = iso(end);
}

async function loadStatus() {
  try {
    const health = await api("/api/health");
    const strip = $("#status-strip");
    strip.replaceChildren();

    state.hasLivePricing = health.config.live_provider !== "deeplink";
    // 金鑰可能來自這台瀏覽器,也可能來自伺服器的 .env —— 兩者都算接上了。
    const pricing = Boolean(loadKeys().token) || health.config.cached_prices;
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
        "打開上方的「查價金鑰」填入 Travelpayouts token,就能自動比價排名。" +
          "在那之前站台仍會列出所有可行的行程組合,每一列都有連結可以直接去看真價。",
        "info",
        "no-token"
      );
    }
  } catch (error) {
    $("#status-text").textContent = "後端沒有回應";
  }
}

/** 只顯示金鑰的頭尾,中間遮掉 —— 讓你確認填的是哪一組,又不必把它整串
 *  攤在螢幕上。 */
function maskKey(token) {
  if (!token) return "";
  return token.length <= 8 ? "••••" : `${token.slice(0, 4)}…${token.slice(-4)}`;
}

function renderKeyState() {
  const { token, marker } = loadKeys();
  const state_ = $("#key-state");
  if (token) {
    state_.className = "keys__state keys__state--ok";
    state_.textContent =
      `已儲存 token ${maskKey(token)}` + (marker ? ` · marker ${marker}` : " · 未填 marker");
  } else {
    state_.className = "keys__state";
    state_.textContent = "尚未填入,目前沒有價格可以排名";
  }
  $("#key-token").value = token || "";
  $("#key-marker").value = marker || "";
}

function wireKeyPanel() {
  renderKeyState();

  $("#key-save").addEventListener("click", () => {
    const token = $("#key-token").value.trim();
    const marker = $("#key-marker").value.trim();
    saveKeys({ token, marker });
    renderKeyState();
    loadStatus();
    if (token) {
      $("#keys").open = false;
      showNotice("金鑰已儲存", "再按一次「找組合」就會帶著它去查價。", "info", "keys-saved");
    }
  });

  $("#key-clear").addEventListener("click", () => {
    localStorage.removeItem(KEY_STORE);
    renderKeyState();
    loadStatus();
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

function newStop(country) {
  return { country, selected: new Set(), nights_min: 3, nights_max: 4, hop: "surface", label: "" };
}

async function init() {
  defaultDates();
  const body = await api("/api/ref/countries");
  state.countries = body.countries;

  // 預設就是這個專案的驗證行程:台北 → 日本兩城開口。
  const japan = newStop("JP");
  const japan2 = newStop("JP");
  state.stops = [japan, japan2];

  const jpCities = await airportsFor("JP");
  const tokyo = jpCities.find((c) => c.code === "TYO");
  const osaka = jpCities.find((c) => c.code === "OSA");
  if (tokyo) {
    tokyo.airports.forEach((a) => japan.selected.add(a.code));
    japan.label = tokyo.name;
  }
  if (osaka) {
    osaka.airports.forEach((a) => japan2.selected.add(a.code));
    japan2.label = osaka.name;
    japan2.nights_min = 2;
    japan2.nights_max = 3;
  }

  wireKeyPanel();
  wirePasswordPanel();
  await renderPlan();
  $("#plan").addEventListener("submit", runSearch);
  $("#add-stop").addEventListener("click", () => {
    if (state.stops.length >= 3) {
      showNotice("停留點滿了", "最多支援 3 個停留點,再多組合會多到查不動。", "info");
      return;
    }
    state.stops.push(newStop(state.stops.at(-1)?.country || "JP"));
    renderPlan();
  });
  loadStatus();
}

init().catch((error) => showNotice("啟動失敗", error.message, "alert"));
