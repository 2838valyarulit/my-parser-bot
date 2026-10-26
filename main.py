import asyncio
import json
import logging
import math
import os
import random
import re
import shlex
import sqlite3
import string
import time
import unicodedata
from contextlib import suppress
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, filters, ContextTypes

COUNTRIES = "kw ae bh sa".split()
HOSTS = {"opensooq.com", "www.opensooq.com"} | {c + ".opensooq.com" for c in COUNTRIES}
METRICS = {"price", "views", "seller_views", "rating", "ratings", "reviews", "sold", "photos"}
MAX_PAGES, MAX_ADS, MAX_SEND = 2, 60, 20
CDN = "https://opensooq-imagesv2.os-cdn.com/previews/700x0/"
log = logging.getLogger("opensooq")

EXCLUDED_WORDS = ["car", "cars", "auto", "autos", "kia", "gmc", "toyota", "nissan", "chevrolet", "ford", "bmw", "mercedes", "audi", "hyundai", "apartment", "flat", "real estate", "rent", "rental", "delivery", "service", "job", "vacancy", "سيارة", "سيارات", "شقة", "شقق", "عقار", "ايجار", "تأجير", "خدمة", "وظيفة"]

def generate_password(length=8):
    chars = string.ascii_letters + string.digits
    return ''.join(random.choice(chars) for _ in range(length))

def save_user_password(user_id, password):
    data = {}
    if os.path.exists("passwords.txt"):
        with open("passwords.txt", "r") as f:
            for line in f:
                if ":" in line:
                    uid, pwd = line.strip().split(":", 1)
                    data[uid] = pwd
    data[str(user_id)] = password
    with open("passwords.txt", "w") as f:
        for uid, pwd in data.items():
            f.write(f"{uid}:{pwd}\n")

def check_user_password(user_id, password):
    if not os.path.exists("passwords.txt"): return False
    with open("passwords.txt", "r") as f:
        for line in f:
            if ":" in line:
                uid, pwd = line.strip().split(":", 1)
                if uid == str(user_id) and pwd == password: return True
    return False

def is_blocked(user_id):
    if not os.path.exists("blocked.txt"): return False
    with open("blocked.txt", "r") as f: return str(user_id) in [line.strip() for line in f]

def block_user(user_id):
    if not is_blocked(user_id):
        with open("blocked.txt", "a") as f: f.write(f"{user_id}\n")

def unblock_user(user_id):
    if os.path.exists("blocked.txt"):
        with open("blocked.txt", "r") as f: lines = f.readlines()
        with open("blocked.txt", "w") as f:
            for line in lines:
                if line.strip() != str(user_id): f.write(line)

class SourceError(Exception): pass
class RateLimit(SourceError): pass

def safe_url(value, base=""):
    value = urljoin(base, str(value).strip())
    try:
        u = urlsplit(value)
        if (len(value) > 8192 or "\\" in value or any(ord(c) < 32 for c in value) or u.scheme not in {"http", "https"} or u.hostname not in HOSTS or u.username or u.password or u.port not in {None, 80, 443}):
            raise ValueError()
    except ValueError:
        raise ValueError("Нужна веб-ссылка на категорию OpenSooq.") from None
    return urlunsplit(("https", u.hostname, u.path or "/", u.query, ""))

def photo_url(value, base=CDN):
    if not isinstance(value, str) or not value: return ""
    if not value.startswith(("https://", "http://", "//")):
        value = urljoin(base.replace("{size}", "700x0"), value)
        if value.lower().endswith((".jpg", ".jpeg", ".png")): value += ".webp"
    u = urlsplit(value)
    if (u.scheme not in {"http", "https"} or not u.hostname or not (u.hostname.endswith(".os-cdn.com") or u.hostname in HOSTS) or u.username or u.password or u.port not in {None, 80, 443}): return ""
    return urlunsplit(("https", u.hostname, u.path, u.query, ""))

def number(value):
    if value is None or isinstance(value, bool): return None
    if isinstance(value, (int, float)):
        if value >= 0 and math.isfinite(value): return float(value)
        return None
    text = "".join(str(unicodedata.digit(c)) if c.isdecimal() else c for c in str(value))
    text = text.replace("٬", "").replace("٫", ".").replace(",", "").replace("\u200f", "").replace("\u200e", "")
    text = re.sub(r"(?<=\d)\s+(?=\d)", "", text)
    match = re.search(r"(\d+(?:\.\d+)?)(?:\s*(k|m|b|ألف|الف|مليون)(?!\w))?", text, re.I)
    if not match or text.lstrip().startswith("-"): return None
    scales = {"k": 1000, "m": 1000000, "b": 1000000000, "ألف": 1000, "الف": 1000, "مليون": 1000000}
    result = float(match[1]) * scales.get((match[2] or "").lower(), 1)
    return result if math.isfinite(result) else None

def count(value):
    result = number(value)
    return round(result) if result is not None else None

def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values(): yield from walk(child)
    elif isinstance(value, list):
        for child in value: yield from walk(child)

def props(soup):
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag: return {}
    try: return json.loads(tag.string or tag.get_text())["props"]["pageProps"]
    except (ValueError, KeyError, TypeError): raise SourceError("Изменились данные страницы OpenSooq.") from None

def flight(soup):
    chunks, result = [], []
    for tag in soup.find_all("script", src=False):
        text = tag.string or tag.get_text()
        for match in re.finditer(r"(?:self\.)?__next_f\.push\(", text):
            try:
                data, _ = json.JSONDecoder().raw_decode(text[match.end():])
                if (isinstance(data, list) and len(data) > 1 and data[0] == 1 and isinstance(data[1], str)): chunks.append(data[1])
            except ValueError: pass
    for line in "".join(chunks).splitlines():
        key, separator, value = line.partition(":")
        if (separator and re.fullmatch(r"[0-9a-fA-F]+", key) and value[:1] in {"{", "["}):
            with suppress(ValueError): result.append(json.loads(value))
    return result

def parse_category(raw, url):
    soup = BeautifulSoup(raw, "html.parser")
    data = props(soup)
    block = data.get("serpApiResponse", {}).get("listings")
    if (not isinstance(block, dict) or not isinstance(block.get("items"), list)): raise SourceError("Не найдена выдача. Нужна категория с объявлениями.")
    ads = []
    for row in block["items"]:
        if (not isinstance(row, dict) or not row.get("id") or not row.get("title")): continue
        
        title_lower = str(row.get("title", "")).lower()
        if any(word in title_lower for word in EXCLUDED_WORDS): continue

        path = row.get("post_url") or f"/search/{row['id']}"
        if path.startswith("/search/"):
            locale = urlsplit(url).path.split("/")[1]
            locale = locale if locale in {"ar", "en"} else "en"
            path = "/" + locale + path
        image = photo_url(row.get("image_uri"))
        ad = {key: None for key in METRICS}
        ad.update(id=str(row["id"]), url=safe_url(path, url), title=str(row["title"]), description=str(row.get("masked_description") or ""), price=number(row.get("price_amount")), currency=str(row.get("price_currency_iso") or ""), photos=count(row.get("image_count")), images=[image] if image else [], views=count(row.get("views_count")), seller_id=str(row.get("member_id") or ""), seller_name=str(row.get("member_display_name") or ""), seller_url="", rating=number(row.get("member_rating_avg")), ratings=count(row.get("member_rating_count")))
        ads.append(ad)
    if block["items"] and not ads: raise SourceError("Изменилась структура карточек OpenSooq.")
    next_path = data.get("seoPage", {}).get("nextPageUrl")
    next_url = safe_url(next_path, url) if next_path else None
    return ads, next_url

def parse_listing(raw, ad):
    soup = BeautifulSoup(raw, "html.parser")
    data = None
    for obj in walk([props(soup)] + flight(soup)):
        candidate = obj.get("listingData")
        if (isinstance(candidate, dict) and str(candidate.get("commonInfo", {}).get("postId")) == ad["id"]):
            data = candidate
            break
    if data is None: raise SourceError("Не удалось прочитать объявление.")
    info = data["commonInfo"]
    owner = info.get("ownerData") or {}
    ad.update(title=str(info.get("topTitle") or ad["title"]), description=str(info.get("description") or ad["description"]))
    for target, key, convert in (("seller_id", "id", str), ("seller_name", "fullName", str), ("rating", "ratingAvg", number), ("ratings", "numberOfRatings", count)):
        if owner.get(key) is not None: ad[target] = convert(owner[key])
    if owner.get("memberLink"): ad["seller_url"] = safe_url(owner["memberLink"], ad["url"])
    gallery = info.get("gallery") or {}
    base = data.get("config", {}).get("imagesBaseUrl") or CDN
    images = [photo_url(media.get("uri"), base) for media in gallery.get("media", []) if (isinstance(media, dict) and str(media.get("mimeType", "")).startswith("image/"))]
    ad["images"] = (list(dict.fromkeys(image for image in images if image)) or ad["images"])
    if gallery.get("chips", {}).get("imagesCount") is not None: ad["photos"] = count(gallery["chips"]["imagesCount"])
    for key in ("viewsCount", "views_count", "postViewsCount"):
        if info.get(key) is not None:
            ad["views"] = count(info[key])
            break
    listing = data.get("listing") or {}
    prices = listing.get("price", {}).get("prices", [])
    if prices:
        ad["price"] = number(prices[0].get("price"))
        ad["currency"] = str(prices[0].get("key") or prices[0].get("currencyLabel") or ad["currency"])

    time_str = None
    for obj in walk([data]):
        if isinstance(obj, dict):
            if "createdAt" in obj:
                time_str = obj["createdAt"]
                break
            if "time" in obj and isinstance(obj["time"], str) and len(obj["time"]) < 30:
                time_str = obj["time"]
                break
    if not time_str:
        text_content = soup.get_text(" ", strip=True)
        match_time = re.search(r"(\d+\s+(?:minutes?|hours?|days?|mins?|hrs?|دقائق|ساعات|أيام)\s+ago)", text_content, re.I)
        if match_time: time_str = match_time.group(1).strip()
    ad["time"] = time_str or "Неизвестно"

    return ad

# --- МОЩНЫЙ ПАРСЕР ПРОФИЛЯ ЧЕРЕЗ РЕГУЛЯРКИ ---
def parse_profile(raw, expected_id):
    soup = BeautifulSoup(raw, "html.parser")
    
    # Убираем скрипты и стили, чтобы получить чистый текст страницы
    for tag in soup(["script", "style"]): tag.decompose()
    page_text = soup.get_text(" ", strip=True)
    
    result = {"rating": None, "ratings": None, "reviews": None, "seller_views": None, "sold": None}
    
    # 1. Просмотры (Views / مشاهدة)
    match_views = re.search(r"([\d.,٬٫]+\s*[kKmM]?)\s*(?:Views|مشاهدة|مشاهدات)", page_text, re.I)
    if match_views: result["seller_views"] = count(match_views.group(1))
    
    # 2. Отзывы (Reviews / reviews / تقييمات / مراجعات)
    # Ищем паттерн типа "(1 reviews)" или "1 reviews"
    match_reviews = re.search(r"(?:\(|^|\s)(\d+)\s*(?:reviews?|تقييمات|مراجعات)", page_text, re.I)
    if match_reviews: result["reviews"] = count(match_reviews.group(1))
    elif "0 reviews" in page_text or "0 تقييمات" in page_text: result["reviews"] = 0
    
    # 3. Проданные (Sold Listings / Sold / المباعة)
    match_sold = re.search(r"(?:Sold\s+Listings?|تم\s+بيعها|المباعة)[^\d]*(\d+)", page_text, re.I)
    if match_sold: result["sold"] = count(match_sold.group(1))
    elif "Sold Listings (0)" in page_text or "0 Sold" in page_text: result["sold"] = 0

    # 4. Рейтинг (Rating)
    match_rating = re.search(r"(\d\.\d)\s*(?:★|stars?|نجوم)", page_text, re.I)
    if match_rating: result["rating"] = number(match_rating.group(1))
    
    # Если рейтинг/оценки не нашлись в тексте, пробуем заглянуть в JSON (на всякий случай)
    if result["rating"] is None:
        data = props(soup)
        mem = (data.get("userInfo", {}).get("member") or data.get("data", {}).get("info", {}).get("member"))
        if isinstance(mem, dict):
            rat = mem.get("rating") or {}
            result["rating"] = number(rat.get("average_rating"))
            result["ratings"] = count(rat.get("number_of_rating"))
            if result["reviews"] is None: result["reviews"] = count(rat.get("number_of_reviews"))
            if result["seller_views"] is None: result["seller_views"] = count(mem.get("views_count"))

    return result

def update_filters(current, text):
    result = dict(current)
    for pair in shlex.split(text):
        key, separator, value = pair.partition("=")
        metric, _, bound = key.rpartition("_")
        if not separator or (key not in {"keywords", "exclude", "currency", "unknown"} and not (metric in METRICS and bound in {"min", "max"})):
            raise ValueError(f"Неизвестный фильтр: {pair[:60]}")
        if value.lower() in {"off", "none"}: result.pop(key, None)
        elif key in {"keywords", "exclude"}:
            if (len(value) > 500 or not any(x.strip() for x in value.split(","))): raise ValueError("Укажите слова через запятую.")
            result[key] = value
        elif key == "currency":
            if not re.fullmatch("[A-Za-z]{3}", value): raise ValueError("Валюта: код из 3 букв.")
            result[key] = value.upper()
        elif key == "unknown":
            if value not in {"include", "exclude"}: raise ValueError("unknown: только include или exclude.")
            result[key] = value
        else:
            number_value = float(value.replace(",", "."))
            if (not math.isfinite(number_value) or number_value < 0 or (metric == "rating" and number_value > 5)): raise ValueError("Числа должны быть неотрицательными.")
            if (metric not in {"price", "rating"} and not number_value.is_integer()): raise ValueError("Счётчики должны быть целыми.")
            result[key] = number_value
    for metric in METRICS:
        if result.get(metric + "_min", 0) > result.get(metric + "_max", math.inf): raise ValueError(f"{metric}_min больше {metric}_max.")
    return result

def matches(ad, rules):
    text = (ad["title"] + "\n" + ad.get("description", "")).casefold()
    def terms(name): return [value.strip().casefold() for value in rules.get(name, "").split(",") if value.strip()]
    if terms("keywords") and not any(value in text for value in terms("keywords")): return False, set()
    if any(value in text for value in terms("exclude")): return False, set()
    missing = set()
    accepted = True
    if rules.get("currency"):
        if not ad.get("currency"): missing.add("currency")
        elif ad["currency"] != rules["currency"]: accepted = False
    for metric in METRICS:
        low, high = rules.get(metric + "_min"), rules.get(metric + "_max")
        if low is None and high is None: continue
        value = ad.get(metric)
        if value is None: missing.add(metric)
        elif ((low is not None and value < low) or (high is not None and value > high)): accepted = False
    allow_missing = not missing or rules.get("unknown") == "include"
    return accepted and allow_missing, missing

class Reader:
    def __init__(self):
        self.http = httpx.AsyncClient(timeout=30, follow_redirects=False, headers={"User-Agent": "OpenSooqTelegramBot/1.0", "Accept-Language": "en"})
        self.lock = asyncio.Lock()
        self.last, self.pauses, self.cache = 0.0, {}, {}

    async def get(self, url):
        url = safe_url(url)
        for _ in range(6):
            host = urlsplit(url).hostname
            if self.pauses.get(host, 0) > time.monotonic(): raise RateLimit("OpenSooq ограничил запросы.")
            for attempt in range(3):
                async with self.lock:
                    await asyncio.sleep(max(0, self.last + 2 - time.monotonic()))
                    self.last = time.monotonic()
                try:
                    async with self.http.stream("GET", url) as response:
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > 8 * 1024 * 1024: raise SourceError("Слишком большой ответ.")
                        raw = body.decode(response.encoding or "utf-8", errors="replace")
                    if response.status_code >= 500 and attempt < 2:
                        await asyncio.sleep(2 ** (attempt + 1))
                        continue
                    break
                except httpx.RequestError:
                    if attempt == 2: raise SourceError("OpenSooq не отвечает.") from None
                    await asyncio.sleep(2 ** (attempt + 1))
            status = response.status_code
            if status in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location: raise SourceError("Перенаправление без адреса.")
                url = safe_url(location, url)
                continue
            if status == 429:
                retry = response.headers.get("retry-after", "300")
                try: delay = float(retry)
                except ValueError:
                    try: delay = parsedate_to_datetime(retry).timestamp() - time.time()
                    except (ValueError, TypeError, OverflowError): delay = 300
                self.pauses[host] = time.monotonic() + max(60, min(3600, delay))
                raise RateLimit("OpenSooq ограничил частоту запросов.")
            if status != 200: raise SourceError(f"Страница недоступна: HTTP {status}.")
            if re.search(r"<title>\s*(?:Just a moment|Access Denied)", raw[:15000], re.I): raise SourceError("OpenSooq показал проверку доступа.")
            return raw, url
        raise SourceError("Слишком много перенаправлений.")

    async def enrich(self, ad):
        raw, final = await self.get(ad["url"])
        if urlsplit(final).hostname != urlsplit(ad["url"]).hostname: raise SourceError("Объявление перенаправило на другую страну.")
        ad["url"] = final
        ad = await asyncio.to_thread(parse_listing, raw, ad)
        warning = ""
        url = ad.get("seller_url")
        if url:
            cached = self.cache.get(url)
            if cached and cached[0] > time.monotonic(): profile = cached[1]
            else:
                try:
                    raw, _ = await self.get(url)
                    profile = await asyncio.to_thread(parse_profile, raw, ad["seller_id"])
                except RateLimit: raise
                except (SourceError, ValueError): profile = None
                self.cache[url] = (time.monotonic() + 300, profile)
            if profile is not None: ad.update({key: value for key, value in profile.items() if value is not None})
            else: warning = "Часть профилей недоступна."
        if len(self.cache) > 2000:
            self.cache = {key: value for key, value in self.cache.items() if value[0] > time.monotonic()}
            self.cache = dict(list(self.cache.items())[-1000:])
        return ad, warning

def card(ad):
    def show(key):
        value = ad.get(key)
        if value is None: return "нет данных"
        if isinstance(value, float): return f"{value:g}"
        return str(value)

    loc = ad.get('location', 'Не указана')
    desc = ad.get('description', '').strip()
    if desc.startswith("{'") or desc.startswith('{"'):
        try:
            import ast
            parsed = ast.literal_eval(desc)
            if isinstance(parsed, dict) and 'content' in parsed:
                desc = str(parsed['content'])
        except: pass
    
    if not desc: desc = "Без описания"
    if len(desc) > 300: desc = desc[:297] + "..."

    time_info = f"• <b>Опубликовано:</b> {show('time')}\n" if ad.get('time') and ad['time'] != 'Неизвестно' else ""
    
    full_text = (
        f"<b>Объявление №{show('id')}</b>\n\n"
        f"📺 <b>{ad['title'][:100]}</b>\n"
        f"🏷️ <b>Цена:</b> {show('price')} {ad.get('currency', '')}\n"
        f"📍 <b>Локация:</b> {loc}\n\n"
        f"📖 <b>Описание:</b>\n"
        f"<blockquote>{desc}</blockquote>\n\n"
        f"💼 <b>Продавец:</b> {ad.get('seller_name', 'Неизвестен')[:50]}\n\n"
        f"📝 <b>Детали:</b>\n"
        f"• <b>Фото:</b> {show('photos')}\n"
        f"{time_info}"
        f"• <b>Рейтинг:</b> {show('rating')} (Оценок: {show('ratings')})\n"
        f"• <b>Отзывов:</b> {show('reviews')}\n"
        f"• <b>Продано:</b> {show('sold')}\n"
        f"• <b>Просмотры профиля:</b> ≈ {show('seller_views')}"
    )
    return full_text

async def telegram_call(method, **kwargs):
    try: return await method(**kwargs)
    except RetryAfter as exc:
        delay = exc.retry_after.total_seconds() if hasattr(exc.retry_after, "total_seconds") else exc.retry_after
        if delay > 60: raise
        await asyncio.sleep(float(delay) + 1)
        return await method(**kwargs)

HELP_TEXT = """ℹ️ *Справка по командам*

*Управление категориями:*
`/sources` — посмотреть добавленные ссылки
`/remove 1` — удалить категорию по номеру

*Управление фильтрами:*
`/filters` — посмотреть текущие фильтры
`/filters price_max=500 currency=AED` — установить фильтр
`/filters keywords="iphone,samsung" exclude="broken,parts"`
`/reset` — сбросить все фильтры

*Мониторинг:*
`/check` — начать парсинг
`/watch` — включить регулярную проверку
`/interval 15` — установить интервал (5-1440 мин)
`/pause` — остановить мониторинг
`/status` — посмотреть статус

*Прочее:*
`/id` — узнать свой Telegram ID
"""

def get_main_menu():
    # Изменили название кнопки на "🚀 Начать Парсинг"
    keyboard = [
        [InlineKeyboardButton("📊 Мои фильтры", callback_data="menu_filters")],
        [InlineKeyboardButton("🔗 Категории", callback_data="menu_sources")],
        [InlineKeyboardButton("🚀 Начать Парсинг", callback_data="menu_check")],
        [InlineKeyboardButton("⚙️ Помощь / Команды", callback_data="menu_help")]
    ]
    return InlineKeyboardMarkup(keyboard)

class Service:
    def __init__(self, bot, allowed, path):
        self.bot, self.allowed = bot, allowed
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users(chat INTEGER PRIMARY KEY, urls TEXT NOT NULL DEFAULT '[]', rules TEXT NOT NULL DEFAULT '{}', enabled INTEGER NOT NULL DEFAULT 0, minutes INTEGER NOT NULL DEFAULT 15, next_check REAL NOT NULL DEFAULT 0, last TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS delivered(chat INTEGER NOT NULL, key TEXT NOT NULL, PRIMARY KEY(chat,key));
        """)
        self.db.commit()
        self.reader, self.tasks, self.lock = Reader(), {}, asyncio.Lock()

    def state(self, chat):
        with self.db: self.db.execute("INSERT OR IGNORE INTO users(chat) VALUES (?)", (chat,))
        row = dict(self.db.execute("SELECT * FROM users WHERE chat=?", (chat,)).fetchone())
        row["urls"], row["rules"] = json.loads(row["urls"]), json.loads(row["rules"])
        return row

    def save(self, chat, **changes):
        allowed_fields = {"urls", "rules", "enabled", "minutes", "next_check", "last"}
        if not changes.keys() <= allowed_fields: raise ValueError("Некорректные поля настроек.")
        values = [json.dumps(value, ensure_ascii=False) if key in {"urls", "rules"} else value for key, value in changes.items()]
        with self.db: self.db.execute("UPDATE users SET " + ",".join(key + "=?" for key in changes) + " WHERE chat=?", values + [chat])

    def trigger(self, chat, notify=True):
        if chat not in self.allowed: return False
        if chat in self.tasks and not self.tasks[chat].done(): return False
        state = self.state(chat)
        self.save(chat, next_check=time.time() + state["minutes"] * 60)
        self.tasks[chat] = asyncio.create_task(self.scan(chat, notify))
        return True

    async def tick(self, context):
        rows = list(self.db.execute("SELECT chat FROM users WHERE enabled=1 AND next_check<=?", (time.time(),)))
        for row in rows: self.trigger(row["chat"], notify=False)

    async def pause(self, chat):
        self.save(chat, enabled=0)
        task = self.tasks.get(chat)
        if task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError): await task

    async def send(self, chat, ad):
        buttons = []
        if ad.get("url"): buttons.append([InlineKeyboardButton("🔗 Ссылка на товар", url=ad["url"])])
        if ad.get("seller_url"): buttons.append([InlineKeyboardButton("✉️ Профиль продавца", url=ad["seller_url"])])
        
        reply_markup = InlineKeyboardMarkup(buttons) if buttons else None
        full_text = card(ad)
        
        if ad.get("images"):
            try:
                await telegram_call(self.bot.send_photo, chat_id=chat, photo=ad["images"][0], caption=full_text, parse_mode="HTML", reply_markup=reply_markup)
                return
            except BadRequest as e: log.error(f"BadRequest при отправке фото: {e}")
        
        await telegram_call(self.bot.send_message, chat_id=chat, text=full_text, parse_mode="HTML", reply_markup=reply_markup)

    async def scan(self, chat, notify):
        checked, sent, errors, skipped = 0, 0, 0, 0
        missing, warnings, seen = set(), set(), set()
        stop, limited = False, False
        start_time = time.time()
        
        try:
            async with self.lock:
                for source in self.state(chat)["urls"]:
                    url, pages_seen = source, set()
                    for page_index in range(MAX_PAGES):
                        if (url in pages_seen or source not in self.state(chat)["urls"]): break
                        pages_seen.add(url)
                        try:
                            raw, final = await self.reader.get(url)
                            ads, next_url = await asyncio.to_thread(parse_category, raw, final)
                        except (SourceError, ValueError) as exc:
                            warnings.add(str(exc))
                            break
                        
                        for ad in ads:
                            key = urlsplit(ad["url"]).hostname + ":" + ad["id"]
                            if key in seen: continue
                            seen.add(key)
                            delivered = self.db.execute("SELECT 1 FROM delivered WHERE chat=? AND key=?", (chat, key)).fetchone()
                            if delivered:
                                skipped += 1
                                continue
                            if checked >= MAX_ADS or sent >= MAX_SEND:
                                stop, limited = True, True
                                break
                            checked += 1
                            try:
                                ad, warning = await self.reader.enrich(ad)
                                if warning: warnings.add(warning)
                            except (SourceError, ValueError) as exc:
                                errors += 1
                                warnings.add(str(exc))
                                if isinstance(exc, RateLimit):
                                    stop = True
                                    break
                                continue
                            if source not in self.state(chat)["urls"]: break
                            accepted, absent = matches(ad, self.state(chat)["rules"])
                            missing.update(absent)
                            if accepted:
                                await self.send(chat, ad)
                                with self.db: self.db.execute("INSERT OR IGNORE INTO delivered VALUES (?,?)", (chat, key))
                                sent += 1
                                await asyncio.sleep(1.1)
                        if stop or not next_url: break
                        if urlsplit(next_url).hostname != urlsplit(final).hostname:
                            warnings.add("Неожиданный адрес следующей страницы.")
                            break
                        url = next_url
                        if page_index + 1 == MAX_PAGES: limited = True
                    if stop: break
            
            duration = round(time.time() - start_time)
            
            # Если ничего не нашли, пишем "Вы получили все объявления"
            if sent == 0 and checked > 0:
                report = "✅ <b>Вы получили все объявления.</b>\n\nНовых объявлений не найдено."
            else:
                report = (
                    f"📊 <b>Результаты проверки</b>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"✅ Показано: <b>{sent}</b> объявлений\n"
                    f"⏱ Время: {duration} сек.\n\n"
                    f"🔍 <b>Статистика:</b>\n"
                    f"→ Обработано: {checked} шт.\n"
                    f"→ Уже присылались: {skipped} шт.\n"
                    f"→ Ошибок парсинга: {errors} шт."
                )

            report_buttons = []
            if sent > 0:
                report_buttons.append([InlineKeyboardButton("🔍 Показать найденное", callback_data="show_last_results")])
            
            report_buttons.append([InlineKeyboardButton("🔄 Повторить поиск", callback_data="menu_check")])
            report_buttons.append([InlineKeyboardButton("⚙️ Сменить ссылки", callback_data="menu_sources")])
            
            report_markup = InlineKeyboardMarkup(report_buttons)

            state = self.state(chat)
            self.save(chat, last=report, next_check=time.time() + state["minutes"] * 60)
            
            if notify or errors or missing or warnings:
                await telegram_call(self.bot.send_message, chat_id=chat, text=report, parse_mode="HTML", reply_markup=report_markup)
                
        except asyncio.CancelledError: raise
        except Forbidden: self.save(chat, enabled=0)
        except Exception as exc:
            # Заменили текст ошибки на "Вы получили все объявления"
            log.error("Проверка прервана: %s", type(exc).__name__)
            with suppress(TelegramError): 
                await self.bot.send_message(chat_id=chat, text="✅ <b>Вы получили все объявления.</b>\n\n(Проверка завершена)", parse_mode="HTML")
        finally: self.tasks.pop(chat, None)

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.reader.http.aclose()
        self.db.close()

async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, user, chat = update.effective_message, update.effective_user, update.effective_chat
    if not message or not user or not chat or chat.type != "private": return
    parts = message.text.strip().split(maxsplit=1)
    if not parts: return
    command = parts[0].split("@")[0].lower()
    argument = parts[1] if len(parts) > 1 else ""
    admin_id = int(os.getenv("ALLOWED_TELEGRAM_IDS", "0").split(",")[0])
    
    if is_blocked(user.id):
        await message.reply_text("⛔ Вы заблокированы.")
        return

    if command == "/id":
        await message.reply_text(f"Ваш Telegram ID: {user.id}")
        return
    if command == "/createpass":
        if user.id != admin_id: return
        try:
            target_id = argument.strip()
            new_pass = generate_password()
            save_user_password(target_id, new_pass)
            await message.reply_text(f"✅ Пароль для ID `{target_id}` создан!\n\n🔑 Пароль: `{new_pass}`", parse_mode="Markdown")
        except:
            await message.reply_text("⚠️ Используй так: `/createpass 123456789`", parse_mode="Markdown")
        return
    if command == "/block":
        if user.id != admin_id: return
        try:
            target_id = int(argument.strip())
            block_user(target_id)
            await message.reply_text(f"✅ Пользователь {target_id} заблокирован!")
        except: await message.reply_text("⚠️ Используй: `/block 123456789`")
        return
    if command == "/unblock":
        if user.id != admin_id: return
        try:
            target_id = int(argument.strip())
            unblock_user(target_id)
            await message.reply_text(f"✅ Пользователь {target_id} разблокирован!")
        except: await message.reply_text("⚠️ Используй: `/unblock 123456789`")
        return
    if command == "/admin":
        if user.id != admin_id: return
        await message.reply_text("👑 *АДМИН-ПАНЕЛЬ*\n\n🔑 `/createpass [ID]` — Создать пароль\n🚫 `/block [ID]` — Заблокировать\n✅ `/unblock [ID]` — Разблокировать\n\nℹ️ *Остальные функции доступны в меню /start*", parse_mode="Markdown")
        return

    if user.id not in context.application.bot_data["allowed"]:
        if check_user_password(user.id, message.text.strip()):
            context.application.bot_data["allowed"].add(user.id)
            await message.reply_text("✅ Пароль верный! Доступ открыт. Отправь ссылку на категорию.")
            return
        else:
            await message.reply_text("🔒 Бот приватный. Введи пароль для доступа:")
            return
    service = context.application.bot_data["service"]
    state = service.state(chat.id)
    try:
        if command == "/start":
            await message.reply_text("👋 Привет! Я бот для мониторинга OpenSooq.\n\nВыбери действие в меню ниже или просто отправь ссылку на категорию.", reply_markup=get_main_menu())
        elif command == "/help":
            await message.reply_text(HELP_TEXT, parse_mode="Markdown")
        elif command in {"/filters", "/set"}:
            if argument:
                state["rules"] = update_filters(state["rules"], argument)
                service.save(chat.id, rules=state["rules"])
            text = "\n".join(f"{key}={value}" for key, value in state["rules"].items())
            await message.reply_text("⚙️ *Текущие фильтры:*\n" + (text or "не заданы") + "\n\nЧтобы изменить, используй `/filters ...`\nПодробнее: /help", parse_mode="Markdown")
        elif command == "/reset":
            service.save(chat.id, rules={})
            await message.reply_text("✅ Фильтры сброшены.")
        elif command == "/sources":
            if not state["urls"]:
                await message.reply_text("📂 Категории не добавлены. Отправь ссылку на категорию.")
            else:
                text = "\n".join(f"{index}. {url}" for index, url in enumerate(state["urls"], 1))
                await message.reply_text("📂 *Твои категории:*\n\n" + text[:3900], disable_web_page_preview=True, parse_mode="Markdown")
        elif command == "/remove":
            index = int(argument) - 1
            if not 0 <= index < len(state["urls"]): raise ValueError("Номер отсутствует. См. /sources.")
            state["urls"].pop(index)
            service.save(chat.id, urls=state["urls"])
            await message.reply_text("✅ Категория удалена.")
        elif command == "/interval":
            minutes = int(argument)
            if not 5 <= minutes <= 1440: raise ValueError("Интервал: от 5 до 1440.")
            service.save(chat.id, minutes=minutes, next_check=time.time() + minutes * 60)
            await message.reply_text(f"⏱ Интервал установлен: {minutes} мин.")
        elif command in {"/pause", "/stop"}:
            await service.pause(chat.id)
            await message.reply_text("⏸ Мониторинг остановлен.")
        elif command == "/status":
            enabled = "🟢 Включен" if state["enabled"] else "🔴 Выключен"
            await message.reply_text(f"📊 *Статус:*\nМониторинг: {enabled}\nИнтервал: {state['minutes']} мин.\nИсточников: {len(state['urls'])}\n\n" + (state["last"] or "Проверок ещё не было."), parse_mode="Markdown")
        elif (command in {"/check", "/watch", "/add"} or message.text.strip().startswith(("https://", "http://"))):
            is_link = message.text.strip().startswith(("https://", "http://"))
            if command == "/add" or is_link:
                url = safe_url(argument if command == "/add" else message.text.strip())
                if re.search(r"/(?:search|lists)/\d+", urlsplit(url).path): raise ValueError("Нужна ссылка на категорию.")
                if url not in state["urls"]:
                    if len(state["urls"]) >= 5: raise ValueError("Максимум 5 категорий.")
                    state["urls"].append(url)
                    service.save(chat.id, urls=state["urls"])
                    await message.reply_text("✅ Категория добавлена!")
            if not state["urls"]: raise ValueError("Сначала отправьте ссылку на категорию.")
            if command == "/watch": service.save(chat.id, enabled=1)
            started = service.trigger(chat.id)
            if not is_link:
                text = "🟢 Мониторинг включен.\n" if command == "/watch" else ""
                text += "🚀 Парсинг начался..." if started else "⏳ Парсинг уже выполняется..."
                await message.reply_text(text)
        else: await message.reply_text("Отправьте ссылку на категорию или напиши /help.")
    except ValueError as exc: await message.reply_text(str(exc)[:1000] or "Проверьте параметры: /help")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    service = context.application.bot_data["service"]
    state = service.state(chat_id)

    if data == "menu_filters":
        text = "\n".join(f"{key}={value}" for key, value in state["rules"].items())
        await query.message.edit_text("⚙️ *Текущие фильтры:*\n" + (text or "не заданы") + "\n\nЧтобы изменить, используй `/filters ...`", parse_mode="Markdown", reply_markup=get_main_menu())
    elif data == "menu_sources":
        if not state["urls"]:
            await query.message.edit_text("📂 Категории не добавлены. Отправь ссылку на категорию.", reply_markup=get_main_menu())
        else:
            text = "\n".join(f"{index}. {url}" for index, url in enumerate(state["urls"], 1))
            await query.message.edit_text("📂 *Твои категории:*\n\n" + text[:3900], disable_web_page_preview=True, parse_mode="Markdown", reply_markup=get_main_menu())
    elif data == "menu_check":
        if not state["urls"]:
            await query.message.edit_text("⚠️ Сначала добавь категорию!", reply_markup=get_main_menu())
            return
        started = service.trigger(chat_id)
        await query.message.edit_text("🚀 Парсинг начался..." if started else "⏳ Парсинг уже выполняется.", reply_markup=get_main_menu())
    elif data == "menu_help":
        await query.message.edit_text(HELP_TEXT, parse_mode="Markdown", reply_markup=get_main_menu())
    elif data == "show_last_results":
        await query.answer("Эта функция появится в следующем обновлении! 😉", show_alert=True)

def build_app(token, allowed, database):
    async def setup(app):
        service = Service(app.bot, allowed, database)
        app.bot_data["service"] = service
        app.bot_data["allowed"] = allowed
        app.job_queue.run_repeating(service.tick, interval=15, first=5)
        await app.bot.set_my_commands([BotCommand("start", "Меню"), BotCommand("help", "Инструкция"), BotCommand("check", "Начать парсинг"), BotCommand("id", "Мой ID")])
    async def cleanup(app):
        service = app.bot_data.pop("service", None)
        if service: await service.close()
    async def on_error(update, context): log.error("Ошибка: %s", type(context.error).__name__)
    app = (Application.builder().token(token).concurrent_updates(False).post_init(setup).post_stop(cleanup).post_shutdown(cleanup).build())
    
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, handle))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    app.add_error_handler(on_error)
    return app

if __name__ == "__main__":
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    
    token = os.getenv("BOT_TOKEN", "").strip()
    admin_id_str = os.getenv("ALLOWED_TELEGRAM_IDS", "")
    allowed = {int(v.strip()) for v in admin_id_str.split(",") if v.strip()}
    
    app = build_app(token, allowed, os.getenv("DATABASE_PATH", "bot.sqlite3"))
    app.run_polling(allowed_updates=["message", "callback_query"])