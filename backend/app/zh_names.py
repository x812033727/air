"""繁體中文譯名覆寫表。

Travelpayouts 的參考資料只附英文(實測 `name_translations` 全部 253 個國家、
9,644 個城市都只有 `en` 一個語系),所以中文名得自己維護。

這裡只收「台灣旅客真的會選」的地方 —— 亞洲鄰近國家收得密,其他洲收主要門戶。
查不到就回英文原名,不猜、不硬翻:一個亂翻的地名比英文原名更難用。
"""

from __future__ import annotations

COUNTRY_ZH: dict[str, str] = {
    "TW": "台灣", "JP": "日本", "KR": "南韓", "KP": "北韓", "CN": "中國",
    "HK": "香港", "MO": "澳門", "TH": "泰國", "VN": "越南", "SG": "新加坡",
    "MY": "馬來西亞", "ID": "印尼", "PH": "菲律賓", "KH": "柬埔寨", "LA": "寮國",
    "MM": "緬甸", "BN": "汶萊", "IN": "印度", "NP": "尼泊爾", "LK": "斯里蘭卡",
    "BD": "孟加拉", "PK": "巴基斯坦", "MV": "馬爾地夫", "MN": "蒙古",
    "AE": "阿聯", "QA": "卡達", "SA": "沙烏地阿拉伯", "IL": "以色列",
    "TR": "土耳其", "JO": "約旦", "OM": "阿曼", "KZ": "哈薩克", "UZ": "烏茲別克",
    "GE": "喬治亞", "AM": "亞美尼亞", "AZ": "亞塞拜然",
    "US": "美國", "CA": "加拿大", "MX": "墨西哥", "BR": "巴西", "AR": "阿根廷",
    "CL": "智利", "PE": "秘魯", "CO": "哥倫比亞",
    "GB": "英國", "IE": "愛爾蘭", "FR": "法國", "DE": "德國", "NL": "荷蘭",
    "BE": "比利時", "LU": "盧森堡", "CH": "瑞士", "AT": "奧地利", "IT": "義大利",
    "ES": "西班牙", "PT": "葡萄牙", "GR": "希臘", "SE": "瑞典", "NO": "挪威",
    "DK": "丹麥", "FI": "芬蘭", "IS": "冰島", "PL": "波蘭", "CZ": "捷克",
    "HU": "匈牙利", "SK": "斯洛伐克", "SI": "斯洛維尼亞", "HR": "克羅埃西亞",
    "RO": "羅馬尼亞", "BG": "保加利亞", "RS": "塞爾維亞", "UA": "烏克蘭",
    "RU": "俄羅斯", "EE": "愛沙尼亞", "LV": "拉脫維亞", "LT": "立陶宛",
    "MT": "馬爾他", "CY": "賽普勒斯",
    "AU": "澳洲", "NZ": "紐西蘭", "FJ": "斐濟", "PW": "帛琉", "GU": "關島",
    "EG": "埃及", "MA": "摩洛哥", "ZA": "南非", "KE": "肯亞", "TZ": "坦尚尼亞",
    "ET": "衣索比亞", "MU": "模里西斯",
}

CITY_ZH: dict[str, str] = {
    # 台灣
    "TPE": "台北", "KHH": "高雄", "RMQ": "台中", "TNN": "台南", "HUN": "花蓮",
    # 日本
    "TYO": "東京", "OSA": "大阪", "NGO": "名古屋", "FUK": "福岡", "SPK": "札幌",
    "OKA": "沖繩(那霸)", "KIJ": "新潟", "SDJ": "仙台", "HIJ": "廣島",
    "KOJ": "鹿兒島", "KMJ": "熊本", "TAK": "高松", "OKJ": "岡山", "KMQ": "小松",
    "AOJ": "青森", "HKD": "函館", "ISG": "石垣", "MYJ": "松山(愛媛)",
    "TOY": "富山", "FSZ": "靜岡", "NGS": "長崎", "OIT": "大分", "AXT": "秋田",
    # 韓國
    "SEL": "首爾", "PUS": "釜山", "CJU": "濟州", "TAE": "大邱",
    # 中港澳
    "HKG": "香港", "MFM": "澳門", "BJS": "北京", "SHA": "上海", "CAN": "廣州",
    "SZX": "深圳", "CTU": "成都", "XIY": "西安", "CKG": "重慶", "HGH": "杭州",
    "XMN": "廈門", "KMG": "昆明", "TAO": "青島", "NKG": "南京",
    # 東南亞
    "BKK": "曼谷", "CNX": "清邁", "HKT": "普吉", "KBV": "喀比", "USM": "蘇梅島",
    "SIN": "新加坡", "KUL": "吉隆坡", "PEN": "檳城", "BKI": "亞庇", "LGK": "蘭卡威",
    "SGN": "胡志明市", "HAN": "河內", "DAD": "峴港", "CXR": "芽莊", "PQC": "富國島",
    "MNL": "馬尼拉", "CEB": "宿霧", "PPS": "巴拉望", "DPS": "峇里島", "CGK": "雅加達",
    "PNH": "金邊", "REP": "暹粒", "RGN": "仰光", "VTE": "永珍", "BWN": "汶萊",
    # 南亞・中東
    "DEL": "德里", "BOM": "孟買", "KTM": "加德滿都", "CMB": "可倫坡", "MLE": "馬列",
    "DXB": "杜拜", "AUH": "阿布達比", "DOH": "杜哈", "IST": "伊斯坦堡", "TLV": "特拉維夫",
    # 大洋洲
    "SYD": "雪梨", "MEL": "墨爾本", "BNE": "布里斯本", "PER": "伯斯", "OOL": "黃金海岸",
    "AKL": "奧克蘭", "CHC": "基督城", "ZQN": "皇后鎮", "GUM": "關島", "ROR": "帛琉",
    # 北美
    "NYC": "紐約", "LAX": "洛杉磯", "SFO": "舊金山", "SEA": "西雅圖", "CHI": "芝加哥",
    "LAS": "拉斯維加斯", "HNL": "檀香山", "BOS": "波士頓", "WAS": "華盛頓特區",
    "YVR": "溫哥華", "YTO": "多倫多", "YUL": "蒙特婁", "MEX": "墨西哥城",
    # 歐洲
    "LON": "倫敦", "PAR": "巴黎", "ROM": "羅馬", "MIL": "米蘭", "VCE": "威尼斯",
    "FLR": "佛羅倫斯", "NAP": "拿坡里", "BCN": "巴塞隆納", "MAD": "馬德里",
    "LIS": "里斯本", "OPO": "波多", "AMS": "阿姆斯特丹", "BRU": "布魯塞爾",
    "BER": "柏林", "MUC": "慕尼黑", "FRA": "法蘭克福", "HAM": "漢堡",
    "ZRH": "蘇黎世", "GVA": "日內瓦", "VIE": "維也納", "PRG": "布拉格",
    "BUD": "布達佩斯", "WAW": "華沙", "KRK": "克拉科夫", "CPH": "哥本哈根",
    "STO": "斯德哥爾摩", "OSL": "奧斯陸", "HEL": "赫爾辛基", "REK": "雷克雅維克",
    "ATH": "雅典", "DUB": "都柏林", "EDI": "愛丁堡", "MOW": "莫斯科",
    # 非洲
    "CAI": "開羅", "CMN": "卡薩布蘭加", "RAK": "馬拉喀什", "JNB": "約翰尼斯堡",
    "CPT": "開普敦", "NBO": "奈洛比",
}

# 只在機場名字對使用者「選哪個機場」真的有差時才收 —— 東京 NRT/HND、大阪
# KIX/ITM 這種二選一才是重點,一個城市只有一座機場的就不必了。
AIRPORT_ZH: dict[str, str] = {
    "TPE": "桃園", "TSA": "松山",
    "NRT": "成田", "HND": "羽田",
    "KIX": "關西", "ITM": "伊丹", "UKB": "神戶",
    "CTS": "新千歲", "OKD": "丘珠",
    "IBR": "茨城",
    "ICN": "仁川", "GMP": "金浦",
    "PEK": "首都", "PKX": "大興",
    "PVG": "浦東", "SHA": "虹橋",
    "DMK": "廊曼", "BKK": "蘇凡納布",
    "KUL": "吉隆坡國際", "SZB": "梳邦",
    "HKG": "赤鱲角",
    "JFK": "甘迺迪", "EWR": "紐華克", "LGA": "拉瓜迪亞",
    "LHR": "希斯洛", "LGW": "蓋威克", "STN": "史坦斯特", "LTN": "盧頓",
    "CDG": "戴高樂", "ORY": "奧利", "BVA": "波韋",
    "FCO": "菲烏米奇諾", "CIA": "錢皮諾",
    "MXP": "馬爾彭薩", "LIN": "利納特", "BGY": "貝加莫",
    "BCN": "埃爾普拉特",
    "NGO": "中部國際", "NKM": "縣營名古屋",
    "SIN": "樟宜",
}


# 台灣出發的熱門目的地,決定國家選單的預設排序。名單外的國家依名稱排在後面 ——
# 一個從 Abkhazia 開頭的國家選單,技術上正確但沒有人想用。
POPULAR_COUNTRIES: tuple[str, ...] = (
    "JP", "KR", "TH", "VN", "SG", "MY", "HK", "PH", "ID", "MO",
    "CN", "TW", "US", "AU", "NZ", "GB", "FR", "IT", "ES", "DE",
    "NL", "CH", "AT", "CZ", "TR", "AE", "IN", "CA", "KH", "LA",
)

POPULARITY: dict[str, int] = {code: rank for rank, code in enumerate(POPULAR_COUNTRIES)}


def popularity(code: str) -> int:
    """Lower sorts first. Anything unlisted lands after every listed country."""
    return POPULARITY.get(code.upper(), len(POPULAR_COUNTRIES))


def country_name(code: str, fallback: str) -> str:
    return COUNTRY_ZH.get(code.upper(), fallback)


def city_name(code: str, fallback: str) -> str:
    return CITY_ZH.get(code.upper(), fallback)


# `CITY_ZH` is written region by region, most-travelled first within each —
# so its insertion order already encodes how likely a traveller from Taiwan is
# to pick a given city. Reusing it as a rank keeps one hand-maintained list
# instead of two that can drift apart.
CITY_RANK: dict[str, int] = {code: rank for rank, code in enumerate(CITY_ZH)}


def city_rank(code: str) -> int:
    """Lower sorts first; anything unlisted lands after every listed city.

    Sorting by name instead puts 大分, 小松 and 富山 above 福岡 and 沖繩, and
    sorting by airport count alone puts 「Hachijo Jima」 on equal footing with
    them — a tiny island airport counts the same as a major gateway.
    """
    return CITY_RANK.get(code.upper(), len(CITY_RANK))


def is_notable_city(code: str) -> bool:
    """Is this somewhere a traveller from Taiwan would plausibly fly to?"""
    return code.upper() in CITY_ZH


def airport_name(code: str, fallback: str) -> str:
    """機場的中文簡稱;沒收錄就回英文原名。

    回的是簡稱(「成田」而不是「成田國際機場」),因為這個字串出現在一個
    每列都要塞下兩三個機場的表格裡,長名字會把版面吃光。
    """
    return AIRPORT_ZH.get(code.upper(), fallback)
