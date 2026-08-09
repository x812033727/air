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
  keys: null,
  // 沒有即時報價來源時,「查即時價」按鈕只會回一句「未接即時報價來源」——
  // 白費一次點擊,而且旁邊的連結早就在做同一件事。
  hasLivePricing: false,
  // 想搭的航空公司(IATA)。兩個模式共用一份,因為使用者對「我只搭長榮」
  // 這件事的認知不會因為換一個分頁就改變。
  airlines: new Set(),
  airlineNames: new Map(),
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
    // 「這天查無資料」會被讀成「那天沒有飛機」。實際上多半是這條航線只有少數
    // 幾天被人搜過 —— 那些日子就在同一批抓回來的資料裡,講出來就變成下一步。
    for (const leg of missing) {
      box.append(
        gapDetail(leg.gap, leg.alternatives, currency, `${leg.origin}→${leg.destination}`)
      );
    }
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

/** 一個沒有價格的航段,為什麼沒有 —— 以及下一步是什麼。
 *
 *  四種情況在畫面上長得都一樣(一個空格),但要做的事完全不同:沒查過要重跑、
 *  整個月都沒有要換機場、只有遠處有價要整趟挪。全部併成「查無資料」等於把
 *  使用者能做的事一起藏起來。同城替代放在最前面,因為它連日期都不用改。 */
function gapDetail(gap, alternatives, currency, label) {
  const box = el("div");
  if (!gap) return box;

  for (const alt of gap.same_city || []) {
    const line = el("div", "alts alts--strong");
    line.append(el("span", "alts__label", "同城的其他機場那天有票"));
    line.append(
      el("span", "alts__day",
         `${alt.origin}→${alt.destination} ${alt.date.slice(5)} ${money(alt.price, currency)}`)
    );
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

/** 航線上有誰飛。結果畫出來之後才去問,因為這是加值資訊,不該拖慢查價那條主線。
 *  ⚠️ 這不是「那個價格是誰飛的」—— 排名用的資料根本沒有航空公司欄位。 */
async function annotateAirlines(combos) {
  const pairs = [...new Set(
    combos.flatMap((c) => c.legs.map((l) => `${l.origin}-${l.destination}`))
  )].slice(0, 40);
  if (!pairs.length) return;

  let routes;
  try {
    ({ routes } = await api(`/api/routes/airlines?pairs=${pairs.join(",")}`));
  } catch {
    return;  // 純加值,拿不到就算了,不打擾使用者
  }

  for (const node of document.querySelectorAll("[data-route]")) {
    const carriers = routes[node.dataset.route] || [];
    if (!carriers.length) continue;
    node.replaceChildren();
    // 一張卡上有兩三段,標籤不帶航線代碼的話兩行長得一模一樣。
    node.append(el("span", "alts__label", `${node.dataset.route} 有誰飛`));
    for (const carrier of carriers) {
      const chip = el("span", "alts__day", carrier.name);
      chip.title = carrier.code;
      node.append(chip);
    }
  }
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

  // 每段留一個空槽,結果畫出來之後再由 annotateAirlines 填上「這條線有誰飛」。
  for (const leg of combo.legs) {
    const slot = el("div", "alts");
    slot.dataset.route = `${leg.origin}-${leg.destination}`;
    card.append(slot);
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
    airlines: [...state.airlines],
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
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    for (const warning of warmed.warnings || []) {
      showNotice("沒有價格資料", warning, "alert", topicOf(warning));
    }

    button.textContent = "排名中…";
    const result = await api("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
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
    annotateAirlines([...result.results, ...result.unpriceable]);
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
        showNotice("金鑰已儲存", "再按一次「找組合」就會帶著它去查價。", "info", "keys-saved");
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
    if ($("#results").hidden === false || $("#reverse-results").hidden === false) {
      showNotice(
        "航空公司改了",
        "已經在畫面上的連結還是舊的。再按一次「找組合」或「組票」就會套用。",
        "info",
        "airlines-restale"
      );
    }
  });
  paint();
  return chip;
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
  wireAirlinePanel();
  await migrateLegacyKeys();
  await renderKeyState();
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
  role.append(el("b", null, ticket.role));
  if (ticket.open_jaw) role.append(document.createTextNode("開口"));
  if (ticket.pricing) {
    role.append(el("div", "ticket__price",
      ticket.pricing.total != null ? money(ticket.pricing.total, currency) : "查無資料"));
  }
  row.append(role);

  const body = el("div", "ticket__body");

  const line = el("div", "routeline");
  const labels = el("div", "legs");
  const stops = [];
  ticket.legs.forEach((leg, i) => {
    if (i > 0 && ticket.legs[i - 1].destination !== leg.origin) {
      // 票上的缺口。這裡**不**寫「自己走」—— 倒買法的缺口是另一張票飛掉的。
      stops.push({ code: ticket.legs[i - 1].destination, date: null, gapBefore: false });
      stops.push({ code: leg.origin, date: leg.date, gapBefore: true });
    } else if (i > 0) {
      stops.push({ code: leg.origin, date: leg.date });
    } else {
      stops.push({ code: leg.origin, date: leg.date });
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

  const links = el("div", "ticket__links");
  for (const [name, href] of Object.entries(ticket.links)) {
    const link = el("a", "chip",
      { google_flights: "Google Flights", kayak: "Kayak", aviasales: "Aviasales" }[name] || name);
    link.href = href;
    link.target = "_blank";
    link.rel = "noopener";
    links.append(link);
  }
  body.append(links);

  row.append(body);
  return row;
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

  if (plan.risks?.length) {
    const risks = el("ul", "card__risks");
    risks.style.marginTop = "0.8rem";
    for (const risk of plan.risks) risks.append(el("li", null, risk));
    card.append(risks);
  }
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
      }),
    });

    for (const warning of body.warnings || []) {
      showNotice("注意", warning, "alert", topicOf(warning));
    }

    // 只顯示「單程＋反向」。其餘三種後端照樣算、測試照樣守著,
    // 想拿回來比較時只要改這一行。
    const plans = body.groups[0].plans.filter((plan) => plan.method === "hybrid");

    const list = $("#rev-list");
    list.replaceChildren();
    for (const plan of plans) list.append(methodCard(plan, body.currency));

    $("#rev-summary").textContent =
      `${body.route_pairs} 條航線 · ${body.months.join("、")} · ` +
      `這裡只給單程段的快取價,總價請用每張票的連結各自查再比。`;
    $("#reverse-results").hidden = false;
    $("#reverse-results").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    showNotice("組票沒有成功", error.message, "alert");
  } finally {
    button.disabled = false;
    button.textContent = "組票";
  }
}

function wireModes() {
  const tabs = { single: $("#mode-single"), reverse: $("#mode-reverse") };
  const show = (mode) => {
    for (const [name, tab] of Object.entries(tabs)) {
      tab.setAttribute("aria-selected", String(name === mode));
    }
    $("#single-panel").hidden = mode !== "single";
    $("#reverse-panel").hidden = mode !== "reverse";
    for (const id of ["#results", "#baselines", "#gaps"]) {
      if (mode !== "single") $(id).hidden = true;
    }
    if (mode !== "reverse") $("#reverse-results").hidden = true;
  };
  tabs.single.addEventListener("click", () => show("single"));
  tabs.reverse.addEventListener("click", async () => {
    show("reverse");
    if (!$("#reverse-chain").children.length) await initReverse();
  });
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

wireModes();
