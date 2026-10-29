import hashlib
import html
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from http.cookiejar import Cookie, CookieJar
from contextlib import contextmanager
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit
from urllib.parse import urljoin
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
DB_PATH = Path(os.environ.get("DATA_DIR") or ROOT / "data") / "library.sqlite3"
SYNC_MODES = {"manual", "hours", "days", "months"}
DEFAULT_SYNC_MODE = "hours"
DEFAULT_SYNC_INTERVAL = 6
ALLOWED_HOSTS = {"f95zone.to", "www.f95zone.to"}
ALLOWED_IMAGE_HOSTS = {
    *ALLOWED_HOSTS,
    "attachments.f95zone.to",
    "preview.f95zone.to",
    "preview.f95zone.com",
    "preview.f95zone.ninja",
}
MAX_PAGE_BYTES = 4 * 1024 * 1024
MAX_IMAGE_BYTES = 50 * 1024 * 1024
DEV_MODE = os.environ.get("DEV_MODE") == "1"
SYNC_LOCK = threading.Lock()
SETTINGS_CHANGED = threading.Event()
SYNC_PROGRESS_LOCK = threading.Lock()
SYNC_QUEUE = []
SYNC_CURRENT_ID = None
SYNC_PENDING_CHANGES = []
SYNC_REPORT_GAME_IDS = set()
SYNC_PROGRESS = {
    "active": False,
    "completed": 0,
    "total": 0,
    "current_title": "",
    "errors": 0,
    "report_error": "",
}
CATALOG_FILTER_CACHE = {"expires_at": 0, "options": None}
CATALOG_FILTER_LOCK = threading.Lock()
F95_LOCK = threading.Lock()
DEV_VERSION_LOCK = threading.Lock()
DEV_VERSION_SNAPSHOT = None
DEV_VERSION_HASH = ""


def is_allowed_image_host(hostname):
    return bool(hostname) and (
        hostname in ALLOWED_IMAGE_HOSTS
        or hostname.endswith((".f95zone.to", ".f95zone.com", ".f95zone.ninja"))
    )


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_thread_url(value):
    if not isinstance(value, str):
        raise ValueError("Inserisci un URL valido del thread F95Zone.")
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ALLOWED_HOSTS
        or not parsed.path.startswith("/threads/")
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Sono accettati solo link HTTPS a thread su f95zone.to.")
    return f"https://{parsed.hostname}{parsed.path}"


def thread_id_from_url(thread_url):
    segment = urlsplit(thread_url).path.rstrip("/").rsplit("/", 1)[-1]
    match = re.search(r"(?:^|\.)(\d+)$", segment)
    return int(match.group(1)) if match else None


def title_from_thread_url(thread_url):
    slug = urlsplit(thread_url).path.rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"\.\d+$", "", slug)
    title = re.sub(r"[-_]+", " ", slug).strip()
    return title[:300] or "Gioco F95Zone"


class SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
            raise HTTPError(newurl, code, "Redirect F95Zone non consentito", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class SafeImageRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if parsed.scheme != "https" or not is_allowed_image_host(parsed.hostname):
            raise HTTPError(newurl, code, "Redirect immagine non consentito", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class ThreadMetadataParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = HtmlNode("#document", {}, None)
        self.stack = [self.root]
        self.meta = {}
        self.post_times = []
        self._ignored_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs = dict(attrs)
        if tag == "meta":
            key = (attrs.get("property") or attrs.get("name") or "").lower()
            content = attrs.get("content")
            if key and content:
                self.meta[key] = content.strip()
        if tag in {"time", "abbr"}:
            value = attrs.get("datetime") or attrs.get("data-time")
            if value:
                parsed = parse_datetime(value)
                if parsed:
                    self.post_times.append(parsed)
        if tag == "p":
            self._close_open("p")
        elif tag == "li":
            self._close_open("li")
        node = HtmlNode(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)
            if tag in {"script", "style"}:
                self._ignored_depth += 1

    def handle_endtag(self, tag):
        tag = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                if tag in {"script", "style"}:
                    self._ignored_depth = max(0, self._ignored_depth - 1)
                del self.stack[index:]
                return

    def handle_data(self, data):
        if not self._ignored_depth:
            self.stack[-1].children.append(data)

    def _close_open(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return


VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}

BLOCK_TAGS = {
    "address", "article", "blockquote", "br", "dd", "div", "dl", "dt", "fieldset",
    "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
    "header", "hr", "li", "main", "ol", "p", "section", "table", "td", "th", "tr", "ul",
}


class HtmlNode:
    def __init__(self, tag, attrs, parent):
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.children = []

    def classes(self):
        return set(self.attrs.get("class", "").split())

    def descendants(self, tag=None, class_name=None):
        for child in self.children:
            if not isinstance(child, HtmlNode):
                continue
            if (tag is None or child.tag == tag) and (class_name is None or class_name in child.classes()):
                yield child
            yield from child.descendants(tag, class_name)

    def text(self, lines=False, skip_hidden=False):
        parts = []
        for child in self.children:
            if isinstance(child, str):
                parts.append(child)
                continue
            if skip_hidden and (
                "u-hidden" in child.classes()
                or child.attrs.get("aria-hidden") == "true"
            ):
                continue
            is_block = child.tag in BLOCK_TAGS
            if lines and is_block and parts and not parts[-1].endswith("\n"):
                parts.append("\n")
            parts.append(child.text(lines=lines, skip_hidden=skip_hidden))
            if lines and is_block:
                parts.append("\n")
        return "".join(parts)


class LoginFormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.token = ""
        self.login_error = ""
        self._in_error = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag.lower() == "input" and attrs.get("name") == "_xfToken":
            self.token = attrs.get("value", "")
        if tag.lower() == "div" and "blockMessage--error" in attrs.get("class", ""):
            self._in_error = True

    def handle_endtag(self, tag):
        if tag.lower() == "div":
            self._in_error = False

    def handle_data(self, data):
        if self._in_error:
            self.login_error += data


def parse_datetime(value):
    value = (value or "").strip()
    try:
        if re.fullmatch(r"\d{9,12}", value):
            return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat(timespec="seconds")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        pass
    for date_format in (
        "%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y",
        "%b %d, %Y at %I:%M %p", "%B %d, %Y at %I:%M %p",
    ):
        try:
            parsed = datetime.strptime(value, date_format).replace(tzinfo=timezone.utc)
            return parsed.isoformat(timespec="seconds")
        except ValueError:
            continue
    return None


def clean_title(value):
    title = re.sub(r"\s+", " ", value or "").strip()
    title = re.sub(r"\s*[|–-]\s*F95zone.*$", "", title, flags=re.IGNORECASE)
    return title[:300]


def normalize_text(value):
    return re.sub(r"\s+", " ", value or "").replace("\xa0", " ").strip()


def has_prefix_class(node):
    return any(
        name in {"label", "label-append", "labelLink"}
        or name.startswith("pre-")
        for name in node.classes()
    )


def heading_text(node):
    parts = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
        elif not has_prefix_class(child):
            parts.append(heading_text(child))
    return normalize_text(" ".join(parts))


TITLE_PREFIXES = (
    "Unreal Engine", "RPG Maker", "Wolf RPG", "Ren'Py", "RenPy",
    "Unreal", "RPGM", "Unity", "Godot", "HTML", "Java", "WebGL",
    "Adrift", "Flash", "Others", "Completed", "Complete", "Ongoing",
    "Abandoned", "On Hold", "Cancelled", "Collection", "VN",
)


def parse_game_title(raw):
    title = normalize_text(raw)
    prefixes = []
    while title:
        matched = next(
            (
                prefix for prefix in TITLE_PREFIXES
                if re.match(rf"^{re.escape(prefix)}(?=[A-Z]|\s|\[|$)", title, re.I)
            ),
            None,
        )
        if not matched:
            break
        prefixes.append(matched)
        title = title[len(matched):].strip()

    tags = []
    while True:
        match = re.search(r"\s*\[([^\]]+)\]\s*$", title)
        if not match:
            break
        tags.insert(0, normalize_text(match.group(1)))
        title = title[:match.start()].strip()

    version = ""
    creator = ""
    if len(tags) > 1:
        creator = tags[-1]
        version = " ".join(tags[:-1])
    elif tags:
        if re.match(r"^(v?\d|ch\.?\s*\d|chapter|episode|ep\.?\s*\d|s\d|season|final|hotfix|public|beta|alpha|demo|day\s*\d)", tags[0], re.I):
            version = tags[0]
        else:
            creator = tags[0]
    return {
        "title": title or normalize_text(raw),
        "version": version,
        "creator": creator,
        "prefixes": prefixes,
    }


def page_heading(root):
    headings = list(root.descendants("h1"))
    for heading in headings:
        classes = heading.classes()
        if "p-title-value" in classes:
            return heading
    for heading in headings:
        if "p-title-page" in heading.classes():
            return heading
    for heading in headings:
        if heading.parent and "p-title" in heading.parent.classes():
            return heading
    return None


def heading_prefixes(heading):
    if not heading:
        return []
    prefixes = []
    for node in heading.descendants():
        if has_prefix_class(node):
            value = normalize_text(node.text())
            if value and value not in prefixes:
                prefixes.append(value)
    return prefixes


def first_thread_post(root):
    posts = [
        node for node in root.descendants()
        if "message-threadStarterPost" in node.classes()
    ]
    if not posts:
        posts = [node for node in root.descendants() if "message--post" in node.classes()]
    if not posts:
        return None
    post = posts[0]
    bodies = [
        node for node in post.descendants()
        if "bbWrapper" in node.classes()
    ]
    if bodies:
        return post, bodies[0]
    bodies = [node for node in post.descendants() if "message-body" in node.classes()]
    return (post, bodies[0]) if bodies else (post, post)


def absolutize_f95_url(value):
    if not value:
        return ""
    value = value.strip()
    if value.startswith(("data:", "javascript:")):
        return ""
    return urljoin("https://f95zone.to/", value)


def upgrade_f95_image_url(value):
    url = absolutize_f95_url(value)
    if not url:
        return ""
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "http"}:
        return ""
    url = re.sub(
        r"^https?://preview\.f95zone\.(?:to|com|ninja)/",
        "https://attachments.f95zone.to/",
        url,
        flags=re.I,
    )
    url = re.sub(r"\.thumb\.(jpe?g|png|gif|webp|avif)(?=[?#]|$)", r".\1", url, flags=re.I)
    url = re.sub(r"([?&])thumb=\d+(?=&|$)", "", url, flags=re.I)
    url = re.sub(r"/thumb(?:nails?)?/", "/", url, flags=re.I)
    return url


def image_url_identity(image_url):
    normalized_url = upgrade_f95_image_url(image_url) or image_url
    parsed = urlsplit(normalized_url)
    stable_query = sorted(
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() not in {"h", "e", "u"}
    )
    return parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, urlencode(stable_query)


def unique_image_urls(image_urls):
    images = []
    seen = set()
    for image_url in image_urls:
        if not isinstance(image_url, str) or not image_url:
            continue
        identity = image_url_identity(image_url)
        if identity in seen:
            continue
        seen.add(identity)
        images.append(image_url)
    return images


def image_candidates(container):
    if not container:
        return
    for node in container.descendants():
        if node.attrs.get("data-lb-single-image") == "1":
            for zoomer in node.descendants(class_name="lbContainer-zoomer"):
                yield zoomer.attrs.get("data-src", "")
            yield node.attrs.get("data-src", "")
            yield node.attrs.get("href", "")
            for image in node.descendants("img"):
                yield from image_candidates_from_img(image)
        if node.tag == "img" and "bbImage" in node.classes():
            yield from image_candidates_from_img(node)


def image_candidates_from_img(image):
    parent = image.parent
    while parent is not None and not (
        "lbContainer--inline" in parent.classes() or "lbContainer" in parent.classes()
    ):
        parent = parent.parent
    if parent:
        for zoomer in parent.descendants(class_name="lbContainer-zoomer"):
            yield zoomer.attrs.get("data-src", "")
    for attr in ("data-url", "data-fullurl", "data-original", "data-src"):
        yield image.attrs.get(attr, "")
    if image.parent and image.parent.tag == "a":
        yield image.parent.attrs.get("href", "")
    yield image.attrs.get("src", "")


def first_image_url(post_body, og_image):
    images = first_post_image_urls(post_body)
    if images:
        return images[0]
    return upgrade_f95_image_url(og_image)


def first_post_image_urls(post_body):
    images = []
    seen = set()
    for candidate in image_candidates(post_body):
        image_url = upgrade_f95_image_url(candidate)
        if not image_url:
            continue
        parsed = urlsplit(image_url)
        if parsed.scheme != "https" or not is_allowed_image_host(parsed.hostname):
            continue
        if re.search(r"(?:banner|_logo|title[-_]?card)", image_url, re.I):
            continue
        identity = image_url_identity(image_url)
        if identity in seen:
            continue
        seen.add(identity)
        images.append(image_url)
    return images[:40]


def extract_field(text, labels):
    label_pattern = "|".join(re.escape(label) for label in labels)
    other_labels = (
        "developer", "developers", "creator", "author", "publisher", "version",
        "release version", "engine", "status", "game status", "thread status",
        "os", "platform", "language",
    )
    stop_pattern = "|".join(re.escape(label) for label in other_labels)
    match = re.search(
        rf"(?:^|[\n|])\s*(?:{label_pattern})\s*:\s*(.+?)(?=\n|$|\s+(?:{stop_pattern})\s*:)",
        text,
        flags=re.I,
    )
    return normalize_text(match.group(1)).strip(" \t|")[:300] if match else ""


OVERVIEW_STOP_LABELS = (
    "thread updated", "thread update", "updated", "last updated", "last update",
    "update date", "release date", "released", "developer", "developers", "creator",
    "author", "publisher", "version", "release version", "engine", "status",
    "game status", "thread status", "os", "platform", "language", "downloads",
    "download links", "changelog", "change log", "patch notes", "screenshots",
    "gallery",
)


def extract_overview(text):
    text = re.sub(r"\r\n?", "\n", text or "").strip()
    overview = re.search(
        r"(?im)(?:^|\n)\s*(?:overview|game description|description|synopsis|story)\s*:?\s*",
        text,
    )
    if overview:
        text = text[overview.end():]
    stop_labels = "|".join(re.escape(label) for label in OVERVIEW_STOP_LABELS)
    text = re.split(rf"(?i)(?:^|\s)(?:{stop_labels})\s*:", text, maxsplit=1)[0]
    return re.sub(r"\n{2,}", "\n\n", text).strip(" \n:-")[:3000]


def parse_count(value, maximum=500_000_000):
    match = re.fullmatch(r"\s*([\d,]+(?:\.\d+)?)\s*([kmb])?\s*", value or "", re.I)
    if not match:
        return 0
    try:
        count = float(match.group(1).replace(",", ""))
    except ValueError:
        return 0
    multiplier = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
    count *= multiplier.get((match.group(2) or "").lower(), 1)
    return int(count) if 0 < count <= maximum else 0


def thread_page_stats(root, post_node):
    views = 0
    likes = 0
    rating = None
    view_blocks = [
        node.text() for node in root.descendants()
        if {"p-description", "p-title-meta"} & node.classes()
    ]
    for text in view_blocks:
        match = re.search(r"\bViews?:\s*([\d,.]+(?:\.\d+)?\s*[kmb]?)", text, re.I)
        if match and (views := parse_count(match.group(1))):
            break

    for node in root.descendants():
        if node.tag != "dt":
            continue
        parent = node.parent
        scope = parent
        while scope and not (
            {"p-body-header", "p-description", "p-title-meta", "p-title"} & scope.classes()
        ):
            scope = scope.parent
        if not parent or not scope:
            continue
        siblings = parent.children
        try:
            index = siblings.index(node)
        except ValueError:
            continue
        value_node = next(
            (sibling for sibling in siblings[index + 1:] if isinstance(sibling, HtmlNode) and sibling.tag == "dd"),
            None,
        )
        if not value_node:
            continue
        label = normalize_text(node.text()).rstrip(":").lower()
        value = parse_count(
            value_node.text(),
            5_000_000 if label in {"like", "likes", "reaction", "reactions"} else 500_000_000,
        )
        if not views and label in {"view", "views"}:
            views = value
        if not likes and label in {"like", "likes", "reaction", "reactions"}:
            likes = value

    if post_node:
        for node in post_node.descendants():
            if not likes and node.attrs.get("data-reaction-count"):
                likes = parse_count(node.attrs["data-reaction-count"], 5_000_000)
            if not likes and (
                "reactionsBar-link" in node.classes()
                or "reactions" in node.attrs.get("href", "")
            ):
                text = normalize_text(node.text())
                others = re.search(r"\band\s+([\d,.]+)\s+others?\b", text, re.I)
                if others:
                    named = len(list(node.descendants("bdi"))) or min(3, text.count(",") + 1)
                    likes = min(5_000_000, parse_count(others.group(1), 5_000_000) + max(1, named))
                else:
                    likes = parse_count(text, 5_000_000) or parse_count(
                        node.attrs.get("title", ""),
                        5_000_000,
                    )
    for node in root.descendants():
        if rating is not None or not (
            "ratingStarsRow" in node.classes()
            or "ratingValue" in node.classes()
            or node.attrs.get("data-rating")
        ):
            continue
        raw_rating = " ".join(
            node.attrs.get(key, "") for key in ("data-rating", "title", "aria-label")
        ) or node.text()
        match = re.search(r"\b([0-5](?:[.,]\d+)?)\s*(?:/|out of)?\s*5?\b", raw_rating)
        if match:
            try:
                candidate = float(match.group(1).replace(",", "."))
            except ValueError:
                continue
            if 0 <= candidate <= 5:
                rating = candidate
    return {"likes": likes, "views": views, "f95_rating": rating}


def catalog_metric(value, maximum):
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value) if 0 < value <= maximum else 0
    return parse_count(str(value or ""), maximum)


def fetch_catalog_stats(title, thread_url):
    thread_match = re.search(r"/threads/[^/?#]*\.(\d+)", urlsplit(thread_url).path, re.I)
    if not thread_match:
        raise ValueError("Non riesco a ricavare l'ID del thread per leggere le statistiche del catalogo.")
    thread_id = int(thread_match.group(1))
    query = urlencode({
        "cmd": "list",
        "cat": "games",
        "page": "1",
        "sort": "date",
        "rows": "90",
        "ignored": "show",
        "_": str(int(time.time() * 1000)),
        "search": title[:180],
    })
    response, _ = f95_request(
        f"https://f95zone.to/sam/latest_alpha/latest_data.php?{query}",
        headers={
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://f95zone.to/sam/latest_alpha/",
        },
    )
    try:
        payload = json.loads(response.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise ValueError("Il catalogo F95Zone non ha restituito JSON leggibile.") from exc
    message = payload.get("msg") if isinstance(payload, dict) else None
    rows = message.get("data", []) if isinstance(message, dict) else []
    if not isinstance(payload, dict) or payload.get("status") != "ok" or not isinstance(rows, list):
        raise ValueError("La ricerca nel catalogo F95Zone non è riuscita.")
    game = next(
        (
            row for row in rows
            if isinstance(row, dict) and str(row.get("thread_id", "")) == str(thread_id)
        ),
        None,
    )
    if not game:
        return None
    rating_value = game.get("rating")
    try:
        rating = float(rating_value) if rating_value not in (None, "") else None
    except (TypeError, ValueError):
        rating = None
    if rating is not None and not 0 <= rating <= 5:
        rating = None
    return {
        "f95_rating": rating,
        "likes": catalog_metric(game.get("likes"), 5_000_000),
        "views": catalog_metric(game.get("views"), 500_000_000),
    }


def catalog_named_items(value, key):
    results = {}
    if isinstance(value, dict):
        if "id" in value and "name" in value:
            try:
                item_id = int(value["id"])
            except (TypeError, ValueError):
                item_id = 0
            name = html.unescape(str(value.get("name") or "")).strip()
            if item_id > 0 and name:
                results[item_id] = name
        elif value.get(key) is not None:
            results.update(catalog_named_items(value[key], key))
        else:
            for item_key, item in value.items():
                if isinstance(item, str):
                    try:
                        item_id = int(item_key)
                    except (TypeError, ValueError):
                        continue
                    name = html.unescape(item).strip()
                    if item_id > 0 and name:
                        results[item_id] = name
                else:
                    results.update(catalog_named_items(item, key))
    elif isinstance(value, list):
        for item in value:
            results.update(catalog_named_items(item, key))
    return results


CATALOG_PREFIX_GROUPS = {
    "engines": (
        ("adrift", "ADRIFT"),
        ("flash", "Flash"),
        ("godot", "Godot"),
        ("html", "HTML"),
        ("java", "Java"),
        ("others", "Others"),
        ("qsp", "QSP"),
        ("rags", "RAGS"),
        ("rpgm", "RPGM"),
        ("renpy", "Ren'Py"),
        ("tads", "Tads"),
        ("unity", "Unity"),
        ("unrealengine", "Unreal Engine"),
        ("webgl", "WebGL"),
        ("wolfrpg", "Wolf RPG"),
    ),
    "others": (
        ("collection", "Collection"),
        ("siterip", "SiteRip"),
        ("vn", "VN"),
    ),
    "statuses": (
        ("abandoned", "Abandoned"),
        ("completed", "Completed"),
        ("onhold", "Onhold"),
    ),
}


def catalog_prefix_key(name):
    return re.sub(r"[^a-z0-9]+", "", name.casefold())


def catalog_filter_options(prefixes, tags, prefix_group_ids=None):
    prefix_group_ids = prefix_group_ids or {}
    groups = {}
    for group, definitions in CATALOG_PREFIX_GROUPS.items():
        preferred_order = {key: index for index, (key, _) in enumerate(definitions)}
        group_items = []
        for key, item_ids in prefix_group_ids.get(group, {}).items():
            for item_id in item_ids:
                name = prefixes.get(item_id)
                if name:
                    group_items.append((item_id, name, key))
        group_items.sort(
            key=lambda item: (
                0 if item[2] in preferred_order else 1,
                preferred_order.get(item[2], 0),
                item[1].casefold(),
            )
        )
        groups[group] = [{"id": item_id, "name": name} for item_id, name, _ in group_items]

    ordered_prefixes = sorted(prefixes.items(), key=lambda entry: entry[1].casefold())
    return {
        "prefixes": [{"id": item_id, "name": name} for item_id, name in ordered_prefixes],
        "engines": groups["engines"],
        "others": groups["others"],
        "statuses": groups["statuses"],
        "tags": [{"id": item_id, "name": name} for item_id, name in sorted(tags.items(), key=lambda entry: entry[1].casefold())],
    }


def catalog_prefix_data(value, category="games"):
    prefixes = {}
    group_ids = {group: {} for group in CATALOG_PREFIX_GROUPS}
    group_names = {
        "engine": "engines",
        "engines": "engines",
        "other": "others",
        "others": "others",
        "status": "statuses",
        "statuses": "statuses",
    }

    if isinstance(value, dict) and isinstance(value.get(category), (dict, list)):
        value = value[category]

    def visit(node, active_group=None):
        if isinstance(node, dict):
            name = html.unescape(str(node.get("name") or "")).strip()
            normalized_name = re.sub(r"^prefix\s*:\s*", "", name, flags=re.I)
            normalized_name = catalog_prefix_key(normalized_name)
            has_nested_prefixes = isinstance(node.get("prefixes"), (dict, list))
            current_group = group_names.get(normalized_name, active_group) if has_nested_prefixes else active_group
            if current_group and "id" in node and "name" in node and not has_nested_prefixes:
                try:
                    item_id = int(node["id"])
                except (TypeError, ValueError):
                    item_id = 0
                item_name = html.unescape(str(node.get("name") or "")).strip()
                if item_id > 0 and item_name:
                    key = catalog_prefix_key(item_name)
                    prefixes[item_id] = item_name
                    group_ids[current_group].setdefault(key, []).append(item_id)
            for key, child in node.items():
                if isinstance(child, (dict, list)):
                    visit(child, current_group if key == "prefixes" else active_group)
        elif isinstance(node, list):
            for child in node:
                visit(child, active_group)

    visit(value)
    return prefixes, group_ids


def catalog_filter_options_from_msg(message):
    prefixes, group_ids = catalog_prefix_data(message.get("prefixes"))
    tags = catalog_named_items(message.get("tags"), "tags")
    return catalog_filter_options(prefixes, tags, group_ids)


def fetch_catalog_game_details(raw_thread_id):
    try:
        thread_id = int(raw_thread_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("ID del thread non valido.") from exc
    if thread_id <= 0 or thread_id > 9_999_999_999:
        raise ValueError("ID del thread non valido.")
    thread_url = f"https://f95zone.to/threads/{thread_id}/"
    metadata = fetch_thread_metadata(thread_url)
    return {
        **metadata,
        "thread_id": thread_id,
        "thread_url": thread_url,
        "latest_version": metadata.get("version", ""),
    }


def extract_catalog_embedded_json(source, key):
    match = re.search(rf"""["']{re.escape(key)}["']\s*:\s*([\{{\[])""", source)
    if not match:
        return None
    opener = match.group(1)
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for index in range(match.start(1), len(source)):
        char = source[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(source[match.start(1):index + 1])
                except json.JSONDecodeError:
                    return None
    return None


def catalog_filter_options_from_html(source):
    prefix_tree = extract_catalog_embedded_json(source, "prefixes")
    if prefix_tree is None:
        games_tree = re.search(r'\{\s*"games"\s*:\s*\[\s*\{\s*"id"\s*:', source)
        if games_tree:
            prefix_tree = extract_catalog_embedded_json(source[games_tree.start():], "games")
    tag_tree = extract_catalog_embedded_json(source, "tags")
    if tag_tree is None:
        decoded_source = html.unescape(source)
        tag_tree = extract_catalog_embedded_json(decoded_source, "tags")
        if prefix_tree is None:
            prefix_tree = extract_catalog_embedded_json(decoded_source, "prefixes")
    prefixes, group_ids = catalog_prefix_data(prefix_tree)
    tags = catalog_named_items(tag_tree, "tags")
    parser = ThreadMetadataParser()
    parser.feed(source)
    labels_by_for = {
        node.attrs.get("for"): normalize_text(node.text())
        for node in parser.root.descendants("label")
        if node.attrs.get("for") and normalize_text(node.text())
    }
    for node in parser.root.descendants():
        if node.tag == "input" and node.attrs.get("type", "").lower() in {"checkbox", "radio"}:
            input_name = (node.attrs.get("name") or "").lower()
            if "prefix" not in input_name and "tag" not in input_name:
                continue
            raw_id = node.attrs.get("value", "")
            try:
                item_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            label = labels_by_for.get(node.attrs.get("id"), "")
            ancestor = node.parent
            while not label and ancestor and ancestor.tag not in {"fieldset", "form", "body"}:
                if ancestor.tag == "label":
                    label = normalize_text(ancestor.text())
                    break
                label_node = next(
                    (
                        candidate for candidate in ancestor.descendants("label")
                        if normalize_text(candidate.text())
                    ),
                    None,
                )
                if label_node:
                    label = normalize_text(label_node.text())
                    break
                label = normalize_text(
                    ancestor.attrs.get("data-label")
                    or ancestor.attrs.get("aria-label")
                    or node.attrs.get("aria-label")
                    or node.attrs.get("title", "")
                )
                ancestor = ancestor.parent
            if item_id > 0 and label:
                if "prefix" in input_name:
                    prefixes[item_id] = label
                else:
                    tags[item_id] = label
            continue
        if node.tag != "option" and not any(
            attr in node.attrs for attr in ("data-prefix-id", "data-prefix", "data-tag-id", "data-tag")
        ):
            continue
        if "data-prefix-id" in node.attrs or "data-prefix" in node.attrs:
            raw_id = node.attrs.get("data-prefix-id") or node.attrs.get("data-prefix")
            try:
                item_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if item_id > 0 and node.text().strip():
                prefixes[item_id] = html.unescape(node.text().strip())
        if "data-tag-id" in node.attrs or "data-tag" in node.attrs:
            raw_id = node.attrs.get("data-tag-id") or node.attrs.get("data-tag")
            try:
                item_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if item_id > 0 and node.text().strip():
                tags[item_id] = html.unescape(node.text().strip())
        if node.tag == "option" and node.attrs.get("value", "").isdigit() and node.text().strip():
            item_id = int(node.attrs["value"])
            selector_name = (node.parent.attrs.get("name", "") if node.parent else "").lower()
            if "prefix" in selector_name:
                prefixes[item_id] = html.unescape(node.text().strip())
            elif "tag" in selector_name:
                tags[item_id] = html.unescape(node.text().strip())
    return catalog_filter_options(prefixes, tags, group_ids)


def fetch_f95_catalog(query):
    def scalar(name, default):
        value = query.get(name, default)
        return value[0] if isinstance(value, list) and value else value

    try:
        page = max(1, min(int(scalar("page", 1)), 10000))
    except (TypeError, ValueError) as exc:
        raise ValueError("Pagina catalogo non valida.") from exc
    sort = scalar("sort", "date")
    if sort not in {"date", "likes", "views", "title", "rating"}:
        raise ValueError("Ordinamento catalogo non valido.")
    search = str(scalar("search", "")).strip()[:180]
    prefix_type = scalar("prefixtype", "and")
    tag_type = scalar("tagtype", "or")
    date_limit = scalar("date", "0")
    if prefix_type not in {"and", "or"} or tag_type not in {"and", "or"}:
        raise ValueError("Modalità di filtro catalogo non valida.")
    if str(date_limit) not in {"0", "1", "3", "7", "14", "30", "90", "180", "365"}:
        raise ValueError("Limite data catalogo non valido.")
    creator = str(scalar("creator", "")).strip()[:180]
    ignored = scalar("ignored", "hide")
    if ignored not in {"hide", "show"}:
        raise ValueError("Opzione thread ignorati non valida.")
    params = [
        ("cmd", "list"),
        ("cat", "games"),
        ("page", str(page)),
        ("sort", sort),
        ("rows", "30"),
        ("ignored", ignored),
        ("_", str(int(time.time() * 1000))),
    ]
    if str(date_limit) != "0":
        params.append(("date", str(date_limit)))
    if search:
        params.append(("search", search))
    if creator:
        params.append(("creator", creator))
    for query_name, param_name in (
        ("prefixes", "prefixes[]"),
        ("noprefixes", "noprefixes[]"),
        ("tags", "tags[]"),
        ("notags", "notags[]"),
    ):
        values = query.get(query_name, query.get(f"{query_name}[]", []))
        if not isinstance(values, list):
            values = [values]
        if query_name in {"tags", "notags"} and len(values) > 10:
            raise ValueError("Sono consentiti al massimo 10 tag inclusi e 10 esclusi.")
        if len(values) > 100:
            raise ValueError("Sono consentiti al massimo 100 valori per filtro.")
        for value in values:
            try:
                item_id = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("ID filtro catalogo non valido.") from exc
            if item_id <= 0:
                raise ValueError("ID filtro catalogo non valido.")
            params.append((param_name, str(item_id)))
    if query.get("prefixes") or query.get("prefixes[]") or query.get("noprefixes") or query.get("noprefixes[]"):
        params.append(("prefixtype", prefix_type))
    if query.get("tags") or query.get("tags[]") or query.get("notags") or query.get("notags[]"):
        params.append(("tagtype", tag_type))
    response, _ = f95_request(
        "https://f95zone.to/sam/latest_alpha/latest_data.php?" + urlencode(params),
        headers={
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://f95zone.to/sam/latest_alpha/",
        },
    )
    try:
        payload = json.loads(response.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise ValueError("Il catalogo F95Zone non ha restituito JSON leggibile. Verifica la sessione.") from exc
    message = payload.get("msg") if isinstance(payload, dict) else None
    rows = message.get("data") if isinstance(message, dict) else None
    if not isinstance(payload, dict) or payload.get("status") != "ok" or not isinstance(rows, list):
        raise ValueError("La ricerca nel catalogo F95Zone non è riuscita. Verifica il login nelle Impostazioni.")
    filter_options = catalog_filter_options_from_msg(message)

    with database() as db:
        library_thread_ids = set()
        for row in db.execute("SELECT thread_url FROM games").fetchall():
            thread_id = thread_id_from_url(row["thread_url"])
            if thread_id is not None:
                library_thread_ids.add(thread_id)
    games = []
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        try:
            thread_id = int(entry.get("thread_id"))
        except (TypeError, ValueError):
            continue
        thread_url = f"https://f95zone.to/threads/{thread_id}/"
        timestamp = entry.get("ts")
        try:
            updated_at = datetime.fromtimestamp(float(timestamp), timezone.utc).isoformat(timespec="seconds")
        except (TypeError, ValueError, OSError, OverflowError):
            updated_at = ""
        cover = entry.get("cover")
        if isinstance(cover, str) and cover.startswith("//"):
            cover = "https:" + cover
        games.append({
            "thread_id": thread_id,
            "thread_url": thread_url,
            "title": html.unescape(str(entry.get("title") or "Gioco F95Zone")),
            "creator": html.unescape(str(entry.get("creator") or "")),
            "version": html.unescape(str(entry.get("version") or "")),
            "rating": catalog_rating(entry.get("rating")),
            "likes": catalog_metric(entry.get("likes"), 5_000_000),
            "views": catalog_metric(entry.get("views"), 500_000_000),
            "cover_url": cover if isinstance(cover, str) and cover.startswith("https://") else "",
            "updated_at": updated_at,
            "in_library": thread_id in library_thread_ids,
        })
    pagination = message.get("pagination") if isinstance(message, dict) else {}
    pagination = pagination if isinstance(pagination, dict) else {}
    try:
        total_games = int(message.get("count") or len(games))
        total_pages = max(1, int(pagination.get("total") or 1))
        current_page = max(1, int(pagination.get("page") or page))
    except (TypeError, ValueError):
        total_games, total_pages, current_page = len(games), 1, page
    return {
        "games": games,
        "page": current_page,
        "total_pages": total_pages,
        "total_games": total_games,
        "filters": filter_options,
    }


def get_catalog_filter_options(force_refresh=False):
    with CATALOG_FILTER_LOCK:
        if (
            not force_refresh
            and CATALOG_FILTER_CACHE["options"]
            and CATALOG_FILTER_CACHE["expires_at"] > time.time()
        ):
            return CATALOG_FILTER_CACHE["options"]
    page, _ = f95_request(
        "https://f95zone.to/sam/latest_alpha/",
        headers={"Referer": "https://f95zone.to/"},
    )
    page_text = page.decode("utf-8", errors="replace")
    if re.search(r"have to be.{0,80}logged in|must be logged in", page_text, re.I | re.S):
        raise ValueError("Per caricare i filtri del catalogo, accedi a F95Zone dalle Impostazioni.")
    options = catalog_filter_options_from_html(page_text)
    if not options["prefixes"] or not options["tags"]:
        response = fetch_f95_catalog({"page": 1, "sort": "date"})
        list_options = response["filters"]
        prefixes = {item["id"]: item["name"] for item in [*options["prefixes"], *list_options["prefixes"]]}
        tags = {item["id"]: item["name"] for item in [*options["tags"], *list_options["tags"]]}
        options["prefixes"] = [
            {"id": item_id, "name": name}
            for item_id, name in sorted(prefixes.items(), key=lambda entry: entry[1].casefold())
        ]
        options["tags"] = [
            {"id": item_id, "name": name}
            for item_id, name in sorted(tags.items(), key=lambda entry: entry[1].casefold())
        ]
    if not options["prefixes"] or not options["tags"]:
        raise ValueError(
            "SAM non ha restituito l'elenco completo di prefissi e tag. Verifica l'accesso a F95Zone dalle Impostazioni e riprova."
        )
    with CATALOG_FILTER_LOCK:
        CATALOG_FILTER_CACHE.update({"expires_at": time.time() + 900, "options": options})
    return options


def catalog_rating(value):
    try:
        rating = float(value or 0)
    except (TypeError, ValueError):
        return 0
    return rating if 0 <= rating <= 5 else 0


def find_thread_page_one_href(root):
    for node in root.descendants("a"):
        classes = node.classes()
        href = node.attrs.get("href", "")
        if not (
            "pageNav-jump" in classes
            or "pageNav-main" in (node.parent.classes() if node.parent else set())
            or (node.parent and node.parent.parent and "pageNav" in node.parent.parent.classes())
        ):
            continue
        if normalize_text(node.text()) == "1" or re.search(r"page-1(?:/|$)", href, re.I):
            return absolutize_f95_url(href)
    return ""


def parse_thread_metadata(html, thread_url):
    parser = ThreadMetadataParser()
    parser.feed(html)
    root = parser.root
    title_heading = page_heading(root)
    heading_title = heading_text(title_heading) if title_heading else ""
    og_title = clean_title(parser.meta.get("og:title", ""))
    document_title_node = next(root.descendants("title"), None)
    document_title = clean_title(document_title_node.text() if document_title_node else "")
    raw_title = heading_title or og_title or document_title
    title_parts = parse_game_title(clean_title(raw_title))
    prefixes = [*heading_prefixes(title_heading), *title_parts["prefixes"]]
    title = title_parts["title"]

    post = first_thread_post(root)
    post_node, post_body = post if post else (None, None)
    post_text = post_body.text(lines=True, skip_hidden=True) if post_body else ""
    post_text = re.sub(r"[ \t]+\n", "\n", post_text)
    post_text = re.sub(r"\n{2,}", "\n", post_text).strip()

    tags = []
    for node in root.descendants("a"):
        if "tagItem" in node.classes() or (
            node.parent and "js-tagList" in node.parent.classes()
        ):
            tag = normalize_text(node.text())
            if tag and tag not in tags:
                tags.append(tag[:60])

    banner_url = first_image_url(post_body, parser.meta.get("og:image", ""))
    gallery = first_post_image_urls(post_body)
    description = extract_overview(post_text) or parser.meta.get("og:description") or parser.meta.get("description", "")
    description = normalize_text(description)[:3000]
    if not title or re.search(r"\b(log ?in|sign ?in|access denied|verify you are human|just a moment)\b", title, re.I):
        raise ValueError("Non riesco a leggere il thread. Verifica il login F95Zone e riprova.")
    if not title_heading and not og_title and not post_node:
        raise ValueError("La pagina ricevuta non contiene i dati di un thread F95Zone.")

    developer = (
        extract_field(post_text, ["developer", "developers", "creator", "author", "publisher"])
        or title_parts["creator"]
    )
    version = extract_field(post_text, ["version", "release version"]) or title_parts["version"]
    engine = extract_field(post_text, ["engine"])
    if not engine:
        engine = next(
            (prefix for prefix in prefixes if prefix.lower() in {
                "ren'py", "renpy", "rpg maker", "rpgm", "unity", "unreal engine",
                "unreal", "godot", "html", "java", "webgl", "wolf rpg", "adrift", "flash",
            }),
            "",
        )
    game_status = extract_field(post_text, ["game status", "thread status", "status"])
    if not game_status:
        game_status = next(
            (prefix for prefix in prefixes if prefix.lower() in {
                "completed", "complete", "ongoing", "in development", "abandoned",
                "on hold", "cancelled", "released", "early access",
            }),
            "",
        )
    timestamps = parser.post_times
    if post_node:
        timestamps = [
            parsed for node in post_node.descendants()
            if node.tag in {"time", "abbr"}
            for raw in [node.attrs.get("datetime") or node.attrs.get("data-time", "")]
            if (parsed := parse_datetime(raw))
        ] or timestamps
    updated_text = extract_field(
        post_text,
        ["thread updated", "thread update", "last updated", "updated", "last update", "update date"],
    )
    updated_at = parse_datetime(updated_text)
    stats = thread_page_stats(root, post_node)
    return {
        "title": title,
        "image_url": banner_url[:2000],
        "gallery": gallery,
        "description": description,
        "thread_updated_at": updated_at or (max(timestamps) if timestamps else None),
        "developer": developer,
        "version": version,
        "engine": engine,
        "game_status": game_status,
        **stats,
        "tags": tags,
        "page_one_url": find_thread_page_one_href(root),
        "has_first_post": bool(post_node),
    }


def serialize_cookie_jar(cookie_jar):
    return json.dumps([
        {
            "version": cookie.version,
            "name": cookie.name,
            "value": cookie.value,
            "port": cookie.port,
            "port_specified": cookie.port_specified,
            "domain": cookie.domain,
            "domain_specified": cookie.domain_specified,
            "domain_initial_dot": cookie.domain_initial_dot,
            "path": cookie.path,
            "path_specified": cookie.path_specified,
            "secure": cookie.secure,
            "expires": cookie.expires,
            "discard": cookie.discard,
            "comment": cookie.comment,
            "comment_url": cookie.comment_url,
            "rest": cookie._rest,
            "rfc2109": cookie.rfc2109,
        }
        for cookie in cookie_jar
        if cookie.domain.lstrip(".").lower() in ALLOWED_HOSTS
    ])


def has_auth_cookie(cookie_jar):
    current_time = int(time.time())
    return any(
        cookie.name == "xf_user"
        and bool(cookie.value)
        and (cookie.expires is None or cookie.expires > current_time)
        for cookie in cookie_jar
    )


def deserialize_cookie_jar(serialized):
    cookie_jar = CookieJar()
    try:
        cookies = json.loads(serialized or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError("La sessione F95Zone salvata non è leggibile; scollega e accedi di nuovo.") from exc
    if not isinstance(cookies, list):
        raise ValueError("La sessione F95Zone salvata non è valida.")
    for data in cookies:
        domain = data.get("domain") if isinstance(data, dict) else None
        if not isinstance(domain, str) or domain.lstrip(".").lower() not in ALLOWED_HOSTS:
            continue
        cookie_jar.set_cookie(Cookie(
            version=data.get("version", 0),
            name=data["name"],
            value=data.get("value"),
            port=data.get("port"),
            port_specified=data.get("port_specified", False),
            domain=data["domain"],
            domain_specified=data.get("domain_specified", False),
            domain_initial_dot=data.get("domain_initial_dot", False),
            path=data.get("path", "/"),
            path_specified=data.get("path_specified", True),
            secure=data.get("secure", True),
            expires=data.get("expires"),
            discard=data.get("discard", False),
            comment=data.get("comment"),
            comment_url=data.get("comment_url"),
            rest=data.get("rest", {}),
            rfc2109=data.get("rfc2109", False),
        ))
    return cookie_jar


def read_setting(key):
    with database() as db:
        row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def get_sync_settings():
    mode = read_setting("sync_mode") or DEFAULT_SYNC_MODE
    try:
        interval = int(read_setting("sync_interval") or DEFAULT_SYNC_INTERVAL)
    except ValueError:
        mode, interval = DEFAULT_SYNC_MODE, DEFAULT_SYNC_INTERVAL
    if mode not in SYNC_MODES or not 1 <= interval <= 9999:
        mode, interval = DEFAULT_SYNC_MODE, DEFAULT_SYNC_INTERVAL
    return {"sync_mode": mode, "sync_interval": interval}


def validate_sync_settings(data):
    mode = data.get("sync_mode")
    if not isinstance(mode, str) or mode not in SYNC_MODES:
        raise ValueError("Seleziona una frequenza di aggiornamento valida.")
    interval = data.get("sync_interval")
    if mode == "manual" and (isinstance(interval, bool) or not str(interval).isdigit() or not 1 <= int(interval) <= 9999):
        interval = DEFAULT_SYNC_INTERVAL
    if isinstance(interval, bool) or not str(interval).isdigit():
        raise ValueError("Inserisci un numero valido per l'intervallo.")
    interval = int(interval)
    if not 1 <= interval <= 9999:
        raise ValueError("L'intervallo deve essere compreso tra 1 e 9999.")
    return {"sync_mode": mode, "sync_interval": interval}


def sync_interval_seconds(settings):
    unit_seconds = {"hours": 60 * 60, "days": 24 * 60 * 60, "months": 30 * 24 * 60 * 60}
    if settings["sync_mode"] == "manual":
        return None
    return settings["sync_interval"] * unit_seconds[settings["sync_mode"]]


def save_f95_session(cookie_jar, username):
    serialized = serialize_cookie_jar(cookie_jar)
    if not has_auth_cookie(cookie_jar):
        raise ValueError("F95Zone non ha restituito una sessione valida.")
    with database() as db:
        db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('f95_cookies', ?)",
            (serialized,),
        )
        db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('f95_username', ?)",
            (username,),
        )
    secure_database_permissions()


def f95_session_status():
    cookie_jar = deserialize_cookie_jar(read_setting("f95_cookies"))
    logged_in = has_auth_cookie(cookie_jar)
    return {
        "logged_in": logged_in,
        "username": read_setting("f95_username") if logged_in else None,
    }


def load_f95_cookie_jar():
    return deserialize_cookie_jar(read_setting("f95_cookies"))


def perform_f95_request(url, data, headers, cookie_jar):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError("Richiesta verso un host F95Zone non consentito.")
    request_headers = {
        "User-Agent": "F95Library/0.1 (+self-hosted personal library)",
        "Accept": "text/html,application/xhtml+xml",
    }
    if headers:
        request_headers.update(headers)
    request = Request(url, data=data, headers=request_headers)
    opener = build_opener(SafeRedirectHandler(), HTTPCookieProcessor(cookie_jar))
    try:
        with opener.open(request, timeout=20) as response:
            final_url = urlsplit(response.geturl())
            content = response.read(MAX_PAGE_BYTES + 1)
            if len(content) > MAX_PAGE_BYTES:
                raise ValueError("La pagina F95Zone supera il limite di dimensione consentito.")
            if final_url.hostname not in ALLOWED_HOSTS:
                raise ValueError("F95Zone ha reindirizzato a un host non consentito.")
            return content, final_url
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise ValueError("F95Zone ha negato l'accesso. Il thread potrebbe richiedere una sessione valida.") from exc
        raise ValueError(f"F95Zone ha restituito HTTP {exc.code}.") from exc
    except (URLError, TimeoutError) as exc:
        raise ValueError(f"Impossibile raggiungere F95Zone: {exc.reason if isinstance(exc, URLError) else 'timeout'}.") from exc


def f95_request(url, data=None, headers=None, cookie_jar=None):
    if cookie_jar is not None:
        return perform_f95_request(url, data, headers, cookie_jar)
    with F95_LOCK:
        active_cookie_jar = load_f95_cookie_jar()
        result = perform_f95_request(url, data, headers, active_cookie_jar)
        if has_auth_cookie(active_cookie_jar):
            save_f95_session(active_cookie_jar, read_setting("f95_username") or "")
        return result


def fetch_f95_image(image_url):
    parsed = urlsplit(image_url)
    if parsed.scheme != "https" or not is_allowed_image_host(parsed.hostname):
        raise ValueError("Host immagine non consentito.")
    request = Request(
        image_url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; F95Library/0.1)",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Referer": "https://f95zone.to/",
        },
    )
    with F95_LOCK:
        cookie_jar = load_f95_cookie_jar()
        opener = build_opener(
            SafeImageRedirectHandler(),
            HTTPCookieProcessor(cookie_jar),
        )
        try:
            with opener.open(request, timeout=20) as response:
                final = urlsplit(response.geturl())
                if final.scheme != "https" or not is_allowed_image_host(final.hostname):
                    raise ValueError("Redirect immagine F95Zone non consentito.")
                content_type = response.headers.get_content_type()
                if not content_type.startswith("image/"):
                    raise ValueError("F95Zone non ha restituito un'immagine.")
                content = response.read(MAX_IMAGE_BYTES + 1)
                if not content or len(content) > MAX_IMAGE_BYTES:
                    raise ValueError("L'immagine F95Zone è vuota o supera il limite consentito.")
                return content, content_type
        except HTTPError as exc:
            raise ValueError(f"Impossibile caricare l'immagine F95Zone (HTTP {exc.code}).") from exc
        except (URLError, TimeoutError) as exc:
            reason = exc.reason if isinstance(exc, URLError) else "timeout"
            raise ValueError(f"Impossibile raggiungere il CDN immagini F95Zone: {reason}.") from exc


def image_cache_directory():
    return DB_PATH.parent / "images"


def image_cache_filename(image_url):
    digest = hashlib.sha256(image_url.encode("utf-8")).hexdigest()
    return digest


def cache_thread_images(metadata):
    image_urls = unique_image_urls(
        url for url in [metadata.get("image_url", ""), *(metadata.get("gallery") or [])]
        if isinstance(url, str) and url
    )
    errors = []
    image_cache_directory().mkdir(parents=True, exist_ok=True)
    for image_url in image_urls:
        filename = image_cache_filename(image_url)
        with database() as db:
            cached = db.execute(
                "SELECT filename FROM image_cache WHERE source_url = ?",
                (image_url,),
            ).fetchone()
        if cached and (image_cache_directory() / cached["filename"]).is_file():
            continue
        try:
            content, content_type = fetch_f95_image(image_url)
            path = image_cache_directory() / filename
            temporary_path = image_cache_directory() / f"{filename}.tmp"
            temporary_path.write_bytes(content)
            temporary_path.chmod(0o600)
            temporary_path.replace(path)
            with database() as db:
                db.execute(
                    """
                    INSERT INTO image_cache (source_url, filename, content_type, updated_at, last_error)
                    VALUES (?, ?, ?, ?, '')
                    ON CONFLICT(source_url) DO UPDATE SET
                        filename = excluded.filename,
                        content_type = excluded.content_type,
                        updated_at = excluded.updated_at,
                        last_error = ''
                    """,
                    (image_url, filename, content_type, now_iso()),
                )
        except (ValueError, OSError) as exc:
            error = str(exc)
            errors.append(error)
            with database() as db:
                db.execute(
                    """
                    INSERT INTO image_cache (source_url, filename, content_type, updated_at, last_error)
                    VALUES (?, ?, '', ?, ?)
                    ON CONFLICT(source_url) DO UPDATE SET
                        last_error = excluded.last_error,
                        updated_at = excluded.updated_at
                    """,
                    (image_url, filename, now_iso(), error[:500]),
                )
            print(f"Impossibile memorizzare l'immagine F95Zone {image_url}: {error}", flush=True)
    return errors


def get_cached_f95_image(image_url):
    cached = get_cached_f95_image_file(image_url)
    if not cached:
        return None
    path, content_type = cached
    try:
        return path.read_bytes(), content_type
    except FileNotFoundError:
        return None


def get_cached_f95_image_file(image_url):
    with database() as db:
        cached = db.execute(
            "SELECT filename, content_type FROM image_cache WHERE source_url = ? AND last_error = ''",
            (image_url,),
        ).fetchone()
    if not cached:
        return None
    path = image_cache_directory() / cached["filename"]
    try:
        path.stat()
    except FileNotFoundError:
        return None
    return path, cached["content_type"]


def prune_image_cache():
    referenced_urls = set()
    with database() as db:
        rows = db.execute("SELECT image_url, gallery FROM games").fetchall()
        cache_rows = db.execute("SELECT source_url, filename FROM image_cache").fetchall()
    for row in rows:
        if row["image_url"]:
            referenced_urls.add(row["image_url"])
        try:
            gallery = json.loads(row["gallery"])
        except (TypeError, json.JSONDecodeError):
            gallery = []
        if isinstance(gallery, list):
            referenced_urls.update(url for url in gallery if isinstance(url, str) and url)
    stale_rows = [row for row in cache_rows if row["source_url"] not in referenced_urls]
    if not stale_rows:
        return
    with database() as db:
        db.executemany("DELETE FROM image_cache WHERE source_url = ?", [
            (row["source_url"],) for row in stale_rows
        ])
    for row in stale_rows:
        try:
            (image_cache_directory() / row["filename"]).unlink(missing_ok=True)
        except OSError as exc:
            print(f"Impossibile rimuovere l'immagine non più usata {row['filename']}: {exc}", flush=True)


def get_image_cache_errors(image_urls):
    image_urls = list(dict.fromkeys(url for url in image_urls if url))
    if not image_urls:
        return []
    with database() as db:
        rows = db.execute(
            f"""
            SELECT last_error FROM image_cache
            WHERE source_url IN ({','.join('?' for _ in image_urls)}) AND last_error != ''
            """,
            image_urls,
        ).fetchall()
    return [row["last_error"] for row in rows]


def development_source_version():
    global DEV_VERSION_SNAPSHOT, DEV_VERSION_HASH
    source_paths = sorted(
        (
            path for path in ROOT.rglob("*")
            if path.is_file()
            and path.suffix in {".py", ".html", ".css", ".js"}
            and not any(part in {".git", "__pycache__", ".venv", "venv"} for part in path.parts)
        ),
        key=lambda path: str(path.relative_to(ROOT)),
    )
    source_snapshot = []
    for path in source_paths:
        stat = path.stat()
        source_snapshot.append((str(path.relative_to(ROOT)), stat.st_mtime_ns, stat.st_size))
    snapshot = tuple(source_snapshot)
    with DEV_VERSION_LOCK:
        if snapshot != DEV_VERSION_SNAPSHOT:
            digest = hashlib.sha256()
            for path, (relative_path, _, _) in zip(source_paths, snapshot):
                digest.update(relative_path.encode("utf-8"))
                digest.update(path.read_bytes())
            DEV_VERSION_HASH = digest.hexdigest()
            DEV_VERSION_SNAPSHOT = snapshot
        return DEV_VERSION_HASH


def login_f95zone(username, password):
    if not isinstance(username, str) or not username.strip() or len(username) > 120:
        raise ValueError("Inserisci un nome utente F95Zone valido.")
    if not isinstance(password, str) or not password or len(password) > 1024:
        raise ValueError("Inserisci la password F95Zone.")
    login_name = username.strip()
    cookie_jar = CookieJar()
    with F95_LOCK:
        login_page, _ = f95_request(
            "https://f95zone.to/login/",
            cookie_jar=cookie_jar,
        )
        parser = LoginFormParser()
        parser.feed(login_page.decode("utf-8", errors="replace"))
        if not parser.token:
            raise ValueError("Non riesco a leggere il modulo di accesso F95Zone. Riprova più tardi.")
        form_data = urlencode({
            "login": login_name,
            "password": password,
            "remember": "1",
            "_xfRedirect": "/",
            "_xfToken": parser.token,
        }).encode("utf-8")
        response_body, _ = f95_request(
            "https://f95zone.to/login/login",
            data=form_data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://f95zone.to",
                "Referer": "https://f95zone.to/login/",
            },
            cookie_jar=cookie_jar,
        )
        if not has_auth_cookie(cookie_jar):
            error_parser = LoginFormParser()
            error_parser.feed(response_body.decode("utf-8", errors="replace"))
            error_message = re.sub(r"\s+", " ", error_parser.login_error).strip()
            if error_message:
                raise ValueError(error_message[:300])
            raise ValueError("Accesso non riuscito. Controlla le credenziali o verifica se F95Zone richiede una procedura aggiuntiva.")
        save_f95_session(cookie_jar, login_name)
    return f95_session_status()


def logout_f95zone():
    with F95_LOCK, database() as db:
        db.execute("DELETE FROM settings WHERE key IN ('f95_cookies', 'f95_username')")


def fetch_thread_metadata(thread_url):
    content, final_url = f95_request(thread_url)
    if not final_url.path.startswith("/threads/"):
        raise ValueError("F95Zone ha reindirizzato a una pagina non-thread; verifica l'accesso.")
    metadata = parse_thread_metadata(content.decode("utf-8", errors="replace"), thread_url)
    if not metadata["has_first_post"] and metadata["page_one_url"]:
        first_page_content, first_page_url = f95_request(metadata["page_one_url"])
        if first_page_url.path.startswith("/threads/"):
            first_page = parse_thread_metadata(
                first_page_content.decode("utf-8", errors="replace"),
                metadata["page_one_url"],
            )
            if first_page["has_first_post"]:
                metadata = first_page
    metadata.pop("page_one_url", None)
    metadata.pop("has_first_post", None)
    try:
        catalog_stats = fetch_catalog_stats(metadata["title"], thread_url)
    except ValueError as exc:
        print(f"Impossibile leggere le statistiche del catalogo F95Zone per {thread_url}: {exc}", flush=True)
    else:
        if catalog_stats:
            metadata.update(catalog_stats)
    return metadata


def connect_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(DB_PATH, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(descriptor)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    secure_database_permissions()
    return connection


def secure_database_permissions():
    for path in (DB_PATH, Path(f"{DB_PATH}-wal"), Path(f"{DB_PATH}-shm")):
        try:
            path.chmod(0o600)
        except FileNotFoundError:
            pass


@contextmanager
def database():
    connection = connect_db()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_db():
    with database() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_url TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                title_override INTEGER NOT NULL DEFAULT 0,
                developer TEXT NOT NULL DEFAULT '',
                version TEXT NOT NULL DEFAULT '',
                latest_version TEXT NOT NULL DEFAULT '',
                user_version TEXT NOT NULL DEFAULT '',
                user_version_updated_at TEXT,
                game_status TEXT NOT NULL DEFAULT '',
                engine TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '[]',
                image_url TEXT NOT NULL DEFAULT '',
                gallery TEXT NOT NULL DEFAULT '[]',
                description TEXT NOT NULL DEFAULT '',
                thread_updated_at TEXT,
                last_checked_at TEXT,
                last_error TEXT NOT NULL DEFAULT '',
                update_available INTEGER NOT NULL DEFAULT 0,
                personal_status TEXT NOT NULL DEFAULT 'wishlist',
                rating INTEGER,
                f95_rating REAL,
                likes INTEGER NOT NULL DEFAULT 0,
                views INTEGER NOT NULL DEFAULT 0,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS sync_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                games TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS image_cache (
                source_url TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                content_type TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                last_error TEXT NOT NULL DEFAULT ''
            )
            """
        )
        db.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('sync_mode', ?)",
            (DEFAULT_SYNC_MODE,),
        )
        db.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('sync_interval', ?)",
            (str(DEFAULT_SYNC_INTERVAL),),
        )
        db.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('sync_report_epoch', ?)",
            (uuid.uuid4().hex,),
        )
        columns = {row["name"] for row in db.execute("PRAGMA table_info(games)")}
        if "title_override" not in columns:
            db.execute("ALTER TABLE games ADD COLUMN title_override INTEGER NOT NULL DEFAULT 0")
        if "gallery" not in columns:
            db.execute("ALTER TABLE games ADD COLUMN gallery TEXT NOT NULL DEFAULT '[]'")
        for column, definition in (
            ("latest_version", "TEXT NOT NULL DEFAULT ''"),
            ("user_version", "TEXT NOT NULL DEFAULT ''"),
            ("user_version_updated_at", "TEXT"),
            ("f95_rating", "REAL"),
            ("likes", "INTEGER NOT NULL DEFAULT 0"),
            ("views", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in columns:
                db.execute(f"ALTER TABLE games ADD COLUMN {column} {definition}")
        db.execute(
            "UPDATE games SET latest_version = version WHERE latest_version = '' AND version != ''"
        )


def game_to_dict(row):
    game = dict(row)
    if not game.get("latest_version"):
        game["latest_version"] = game.get("version", "")
    game.setdefault("user_version", "")
    game.setdefault("f95_rating", None)
    game.setdefault("likes", 0)
    game.setdefault("views", 0)
    game["description"] = extract_overview(game.get("description", ""))
    try:
        game["tags"] = json.loads(game["tags"])
    except (TypeError, json.JSONDecodeError):
        game["tags"] = []
    try:
        gallery = json.loads(game["gallery"])
    except (TypeError, json.JSONDecodeError):
        gallery = []
    game["gallery"] = unique_image_urls(gallery) if isinstance(gallery, list) else []
    game["update_available"] = bool(game["update_available"])
    return game


def get_game(game_id):
    with database() as db:
        row = db.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    if not row:
        return None
    game = game_to_dict(row)
    game["image_cache_errors"] = get_image_cache_errors(
        [game["image_url"], *game["gallery"]]
    )
    return game


def delete_games(game_ids):
    if not isinstance(game_ids, list) or not game_ids:
        raise ValueError("Seleziona almeno un gioco da rimuovere.")
    if any(isinstance(game_id, bool) or not isinstance(game_id, int) or game_id < 1 for game_id in game_ids):
        raise ValueError("La selezione dei giochi non è valida.")
    game_ids = list(dict.fromkeys(game_ids))
    with SYNC_PROGRESS_LOCK:
        with database() as db:
            existing_ids = set()
            for start in range(0, len(game_ids), 500):
                batch = game_ids[start:start + 500]
                placeholders = ",".join("?" for _ in batch)
                existing_ids.update(
                    row["id"] for row in db.execute(
                        f"SELECT id FROM games WHERE id IN ({placeholders})",
                        batch,
                    ).fetchall()
                )
            if len(existing_ids) != len(game_ids):
                raise ValueError("Uno o più giochi selezionati non sono più presenti nella libreria.")
            for start in range(0, len(game_ids), 500):
                batch = game_ids[start:start + 500]
                placeholders = ",".join("?" for _ in batch)
                db.execute(f"DELETE FROM games WHERE id IN ({placeholders})", batch)
        queued_before = len(SYNC_QUEUE)
        SYNC_QUEUE[:] = [game_id for game_id in SYNC_QUEUE if game_id not in existing_ids]
        SYNC_PROGRESS["total"] -= queued_before - len(SYNC_QUEUE)
        SYNC_REPORT_GAME_IDS.difference_update(existing_ids)
        SYNC_PENDING_CHANGES[:] = [
            game for game in SYNC_PENDING_CHANGES if game["id"] not in existing_ids
        ]
    prune_image_cache()
    return len(game_ids)


GAME_SORT_COLUMNS = {
    "name": "title COLLATE NOCASE",
    "updated": "thread_updated_at",
    "added": "created_at",
    "rating": "COALESCE(f95_rating, 0)",
    "likes": "likes",
    "views": "views",
}


def list_games(
    search="",
    status="all",
    sort="name",
    direction="asc",
    game_statuses=None,
    engines=None,
    tags=None,
    personal_statuses=None,
    exclude_game_statuses=None,
    exclude_engines=None,
    exclude_tags=None,
    exclude_personal_statuses=None,
):
    sort_column = GAME_SORT_COLUMNS.get(sort, GAME_SORT_COLUMNS["name"])
    direction = "DESC" if direction == "desc" else "ASC"
    clauses = []
    params = []
    if search:
        pattern = f"%{search}%"
        clauses.append("(title LIKE ? OR developer LIKE ? OR tags LIKE ?)")
        params.extend((pattern, pattern, pattern))
    if status != "all":
        clauses.append("personal_status = ?")
        params.append(status)
    if game_statuses:
        clauses.append(f"game_status IN ({','.join('?' for _ in game_statuses)})")
        params.extend(game_statuses)
    if engines:
        clauses.append(f"engine IN ({','.join('?' for _ in engines)})")
        params.extend(engines)
    if personal_statuses:
        clauses.append(f"personal_status IN ({','.join('?' for _ in personal_statuses)})")
        params.extend(personal_statuses)
    if exclude_game_statuses:
        clauses.append(f"game_status NOT IN ({','.join('?' for _ in exclude_game_statuses)})")
        params.extend(exclude_game_statuses)
    if exclude_engines:
        clauses.append(f"engine NOT IN ({','.join('?' for _ in exclude_engines)})")
        params.extend(exclude_engines)
    if exclude_personal_statuses:
        clauses.append(f"personal_status NOT IN ({','.join('?' for _ in exclude_personal_statuses)})")
        params.extend(exclude_personal_statuses)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with database() as db:
        rows = db.execute(
            f"""
            SELECT * FROM games {where}
            ORDER BY
                CASE WHEN {sort_column} IS NULL OR {sort_column} = '' THEN 1 ELSE 0 END ASC,
                {sort_column} {direction},
                title COLLATE NOCASE ASC
            """,
            params,
        ).fetchall()
    games = [game_to_dict(row) for row in rows]
    if tags:
        selected_tags = set(tags)
        games = [game for game in games if selected_tags.intersection(game["tags"])]
    if exclude_tags:
        excluded_tags = set(exclude_tags)
        games = [game for game in games if not excluded_tags.intersection(game["tags"])]
    return games


def get_filter_options():
    with database() as db:
        rows = db.execute("SELECT game_status, engine, tags FROM games").fetchall()
    game_statuses = set()
    engines = set()
    tags = set()
    for row in rows:
        if row["game_status"].strip():
            game_statuses.add(row["game_status"].strip())
        if row["engine"].strip():
            engines.add(row["engine"].strip())
        try:
            row_tags = json.loads(row["tags"])
        except (TypeError, json.JSONDecodeError):
            row_tags = []
        if isinstance(row_tags, list):
            tags.update(tag.strip() for tag in row_tags if isinstance(tag, str) and tag.strip())
    return {
        "game_statuses": sorted(game_statuses, key=str.casefold),
        "engines": sorted(engines, key=str.casefold),
        "tags": sorted(tags, key=str.casefold),
    }


def sync_game(game_id, *, prune_cache=True):
    with SYNC_LOCK:
        game = get_game(game_id)
        if not game:
            return None
        try:
            metadata = fetch_thread_metadata(game["thread_url"])
            metadata["gallery"] = unique_image_urls(metadata["gallery"])
            cache_thread_images(metadata)
            old_timestamp = game["thread_updated_at"]
            new_timestamp = metadata["thread_updated_at"]
            is_newer = bool(new_timestamp and old_timestamp and new_timestamp > old_timestamp)
            with database() as db:
                db.execute(
                    """
                    UPDATE games
                    SET title = CASE WHEN title_override = 1 THEN title ELSE ? END,
                        image_url = ?, description = ?,
                        gallery = ?,
                        version = COALESCE(NULLIF(?, ''), version),
                        latest_version = COALESCE(NULLIF(?, ''), latest_version),
                        f95_rating = COALESCE(?, f95_rating),
                        likes = CASE WHEN ? > 0 THEN ? ELSE likes END,
                        views = CASE WHEN ? > 0 THEN ? ELSE views END,
                        developer = CASE WHEN developer = '' THEN ? ELSE developer END,
                        engine = CASE WHEN engine = '' THEN ? ELSE engine END,
                        game_status = COALESCE(NULLIF(?, ''), game_status),
                        tags = CASE WHEN tags = '[]' THEN ? ELSE tags END,
                        thread_updated_at = COALESCE(?, thread_updated_at),
                        last_checked_at = ?, last_error = '',
                        update_available = CASE WHEN ? THEN 1 ELSE update_available END,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        metadata["title"], metadata["image_url"], metadata["description"],
                        json.dumps(unique_image_urls(metadata["gallery"]), ensure_ascii=False),
                        metadata["version"], metadata["version"], metadata.get("f95_rating"),
                        metadata.get("likes", 0), metadata.get("likes", 0),
                        metadata.get("views", 0), metadata.get("views", 0),
                        metadata["developer"], metadata["engine"],
                        metadata["game_status"], json.dumps(metadata["tags"], ensure_ascii=False),
                        new_timestamp, now_iso(), int(is_newer), now_iso(), game_id,
                    ),
                )
            if prune_cache:
                prune_image_cache()
        except ValueError as exc:
            with database() as db:
                db.execute(
                    "UPDATE games SET last_checked_at = ?, last_error = ?, updated_at = ? WHERE id = ?",
                    (now_iso(), str(exc)[:500], now_iso(), game_id),
                )
        return get_game(game_id)


def sync_all_games():
    return queue_sync_jobs()


def get_sync_progress():
    with SYNC_PROGRESS_LOCK:
        return {**SYNC_PROGRESS}


def build_sync_changes(previous, current):
    changes = []
    for field in ("latest_version", "game_status", "title"):
        previous_value = previous.get(field) or (previous.get("version", "") if field == "latest_version" else "")
        current_value = current.get(field) or (current.get("version", "") if field == "latest_version" else "")
        if current_value and previous_value != current_value:
            changes.append({
                "field": field,
                "from": previous_value or "",
                "to": current_value,
            })
    previous_timestamp = previous.get("thread_updated_at")
    current_timestamp = current.get("thread_updated_at")
    if previous_timestamp and current_timestamp and current_timestamp > previous_timestamp:
        changes.append({
            "field": "thread_updated_at",
            "from": previous_timestamp,
            "to": current_timestamp,
        })
    previous_image = previous.get("image_url")
    current_image = current.get("image_url")
    if current_image and previous_image != current_image:
        changes.append({
            "field": "image_url",
            "from": previous_image or "",
            "to": current_image,
        })
    if not changes:
        return None
    return {
        "id": current["id"],
        "title": current["title"],
        "thread_url": current["thread_url"],
        "image_url": current.get("image_url", ""),
        "changes": changes,
    }


def save_sync_report(games):
    if not games:
        return None
    with database() as db:
        cursor = db.execute(
            "INSERT INTO sync_reports (created_at, games) VALUES (?, ?)",
            (now_iso(), json.dumps(games, ensure_ascii=False)),
        )
        return cursor.lastrowid


def get_sync_reports(after_id=0, limit=50):
    with database() as db:
        rows = db.execute(
            "SELECT id, created_at, games FROM sync_reports WHERE id > ? ORDER BY id LIMIT ?",
            (after_id, limit),
        ).fetchall()
        reports = []
        for row in rows:
            try:
                games = json.loads(row["games"])
            except json.JSONDecodeError as exc:
                raise ValueError("Un riepilogo di sincronizzazione salvato non è leggibile.") from exc
            if not isinstance(games, list):
                raise ValueError("Un riepilogo di sincronizzazione salvato non è valido.")
            reports.append({
                "id": row["id"],
                "created_at": row["created_at"],
                "games": games,
            })
        next_after_id = reports[-1]["id"] if reports else after_id
        has_more = db.execute(
            "SELECT 1 FROM sync_reports WHERE id > ? LIMIT 1",
            (next_after_id,),
        ).fetchone() is not None
    return {
        "reports": reports,
        "next_after_id": next_after_id,
        "has_more": has_more,
        "epoch": read_setting("sync_report_epoch"),
    }


def run_sync_queue():
    global SYNC_CURRENT_ID
    while True:
        with SYNC_PROGRESS_LOCK:
            if not SYNC_QUEUE:
                if SYNC_PENDING_CHANGES:
                    try:
                        save_sync_report(SYNC_PENDING_CHANGES)
                    except sqlite3.Error as exc:
                        SYNC_PROGRESS["report_error"] = str(exc)
                        print(f"Errore durante il salvataggio del riepilogo di sincronizzazione: {exc}", flush=True)
                    SYNC_PENDING_CHANGES.clear()
                SYNC_CURRENT_ID = None
                queue_empty = True
            else:
                queue_empty = False
                game_id = SYNC_QUEUE.pop(0)
                SYNC_CURRENT_ID = game_id
        if queue_empty:
            try:
                with SYNC_LOCK:
                    prune_image_cache()
            except (OSError, sqlite3.Error) as exc:
                print(f"Errore durante la pulizia della cache immagini: {exc}", flush=True)
                with SYNC_PROGRESS_LOCK:
                    SYNC_PROGRESS["errors"] += 1
            with SYNC_PROGRESS_LOCK:
                if SYNC_QUEUE:
                    continue
                SYNC_REPORT_GAME_IDS.clear()
                SYNC_PROGRESS["active"] = False
                SYNC_PROGRESS["current_title"] = ""
                return
        game = get_game(game_id)
        with SYNC_PROGRESS_LOCK:
            SYNC_PROGRESS["current_title"] = game["title"] if game else ""
        try:
            previous = game
            result = sync_game(game_id, prune_cache=False)
            if result and result.get("last_error"):
                with SYNC_PROGRESS_LOCK:
                    SYNC_PROGRESS["errors"] += 1
            elif result and previous:
                changes = build_sync_changes(previous, result)
                with SYNC_PROGRESS_LOCK:
                    if changes and game_id in SYNC_REPORT_GAME_IDS:
                        SYNC_PENDING_CHANGES.append(changes)
        except Exception as exc:
            print(f"Errore durante la sincronizzazione del gioco {game_id}: {exc}", flush=True)
            with database() as db:
                db.execute(
                    "UPDATE games SET last_checked_at = ?, last_error = ?, updated_at = ? WHERE id = ?",
                    (now_iso(), str(exc)[:500], now_iso(), game_id),
                )
            with SYNC_PROGRESS_LOCK:
                SYNC_PROGRESS["errors"] += 1
        finally:
            with SYNC_PROGRESS_LOCK:
                SYNC_PROGRESS["completed"] += 1
                SYNC_CURRENT_ID = None


def queue_sync_jobs(game_ids=None, *, report_changes=True):
    global SYNC_CURRENT_ID
    if game_ids is None:
        requested_ids = [game["id"] for game in list_games()]
    else:
        requested_ids = list(dict.fromkeys(int(game_id) for game_id in game_ids))
    with SYNC_PROGRESS_LOCK:
        previously_active = SYNC_PROGRESS["active"]
        queued_ids = set(SYNC_QUEUE)
        if SYNC_CURRENT_ID is not None:
            queued_ids.add(SYNC_CURRENT_ID)
        new_ids = [game_id for game_id in requested_ids if game_id not in queued_ids and get_game(game_id)]
        if not previously_active and not new_ids:
            return {**SYNC_PROGRESS}
        if not previously_active:
            SYNC_PROGRESS.update({
                "active": True,
                "completed": 0,
                "total": 0,
                "current_title": "",
                "errors": 0,
                "report_error": "",
            })
            SYNC_PENDING_CHANGES.clear()
            SYNC_REPORT_GAME_IDS.clear()
        if report_changes:
            SYNC_REPORT_GAME_IDS.update(requested_ids)
        SYNC_QUEUE.extend(new_ids)
        SYNC_PROGRESS["total"] += len(new_ids)
        should_start_worker = not previously_active
        progress = {**SYNC_PROGRESS}
    if should_start_worker:
        try:
            threading.Thread(target=run_sync_queue, daemon=True).start()
        except RuntimeError as exc:
            with SYNC_PROGRESS_LOCK:
                SYNC_QUEUE.clear()
                SYNC_REPORT_GAME_IDS.clear()
                SYNC_PROGRESS["active"] = False
                SYNC_PROGRESS["total"] = SYNC_PROGRESS["completed"]
            raise RuntimeError("Impossibile avviare il processo di aggiornamento.") from exc
    return progress


def sync_worker():
    last_sync = None
    previous_settings = None
    while True:
        settings = get_sync_settings()
        setting_key = (settings["sync_mode"], settings["sync_interval"])
        if setting_key != previous_settings:
            if previous_settings is not None and settings["sync_mode"] != "manual":
                last_sync = time.monotonic()
            previous_settings = setting_key
        if settings["sync_mode"] == "manual":
            SETTINGS_CHANGED.wait()
            SETTINGS_CHANGED.clear()
            continue
        if last_sync is not None:
            interval_seconds = sync_interval_seconds(settings)
            remaining = interval_seconds - (time.monotonic() - last_sync)
            if remaining > 0 and SETTINGS_CHANGED.wait(min(remaining, 24 * 60 * 60)):
                SETTINGS_CHANGED.clear()
                continue
            if remaining > 24 * 60 * 60:
                continue
            SETTINGS_CHANGED.clear()
        try:
            sync_all_games()
        except Exception as exc:
            print(f"Errore durante il controllo periodico F95Zone: {exc}", flush=True)
        last_sync = time.monotonic()


def parse_json_body(handler):
    try:
        length = int(handler.headers.get("Content-Length", "0"))
        if length < 0 or length > 1024 * 1024:
            raise ValueError("Richiesta troppo grande.")
        payload = json.loads(handler.rfile.read(length) or b"{}")
        if not isinstance(payload, dict):
            raise ValueError("Il corpo JSON deve essere un oggetto.")
        return payload
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Corpo JSON non valido.") from exc


def validate_game_fields(data, creating=False):
    allowed = {
        "title", "developer", "game_status", "engine", "tags", "user_version",
        "personal_status", "rating", "notes", "update_available",
    }
    if creating:
        allowed.add("thread_url")
    if not isinstance(data, dict) or set(data) - allowed:
        raise ValueError("Sono presenti campi non supportati.")
    result = {}
    for key in allowed - {"thread_url", "tags", "rating", "update_available"}:
        if key in data:
            if not isinstance(data[key], str) or len(data[key]) > (3000 if key == "notes" else 300):
                raise ValueError(f"Valore non valido per {key}.")
            result[key] = data[key].strip()
    if "title" in result and not result["title"]:
        raise ValueError("Il titolo non può essere vuoto.")
    if "personal_status" in result and result["personal_status"] not in {
        "wishlist", "playing", "completed", "on_hold", "dropped"
    }:
        raise ValueError("Stato della libreria non valido.")
    if "tags" in data:
        tags = data["tags"]
        if not isinstance(tags, list) or len(tags) > 40 or any(not isinstance(tag, str) or len(tag) > 60 for tag in tags):
            raise ValueError("Elenco tag non valido.")
        result["tags"] = json.dumps([tag.strip() for tag in tags if tag.strip()])
    if "rating" in data:
        rating = data["rating"]
        if rating is not None and (not isinstance(rating, int) or isinstance(rating, bool) or rating < 1 or rating > 5):
            raise ValueError("Il voto deve essere un numero da 1 a 5.")
        result["rating"] = rating
    if "update_available" in data:
        if not isinstance(data["update_available"], bool):
            raise ValueError("Il valore dell'aggiornamento non è valido.")
        result["update_available"] = int(data["update_available"])
    return result


def update_game_fields(game_id, fields):
    current = get_game(game_id)
    if not current:
        return None
    updates = dict(fields)
    if "user_version" in updates and updates["user_version"] != current["user_version"]:
        updates["user_version_updated_at"] = now_iso() if updates["user_version"] else None
    if "title" in updates:
        updates["title_override"] = 1
    updates["updated_at"] = now_iso()
    with database() as db:
        db.execute(
            "UPDATE games SET " + ", ".join(f"{key} = ?" for key in updates) + " WHERE id = ?",
            [*updates.values(), game_id],
        )
    return get_game(game_id)


class LibraryHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    def end_headers(self):
        if DEV_MODE:
            self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if DEV_MODE:
            del self.headers["If-Modified-Since"]
            del self.headers["If-None-Match"]
        parts = urlsplit(self.path)
        if parts.path == "/api/image":
            image_url = parse_qs(parts.query).get("url", [""])[0]
            cached_image = get_cached_f95_image_file(image_url)
            if cached_image:
                path, content_type = cached_image
                try:
                    image_file = path.open("rb")
                except FileNotFoundError:
                    image_file = None
                if image_file:
                    with image_file:
                        self.send_response(200)
                        self.send_header("Content-Type", content_type)
                        self.send_header("Content-Length", str(os.fstat(image_file.fileno()).st_size))
                        self.send_header("Cache-Control", "public, max-age=3600")
                        self.send_header("X-Content-Type-Options", "nosniff")
                        self.end_headers()
                        while chunk := image_file.read(64 * 1024):
                            self.wfile.write(chunk)
                    return
            try:
                content, content_type = fetch_f95_image(image_url)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(content)
            return
        if parts.path == "/api/games":
            query = parse_qs(parts.query)
            self.send_json(list_games(
                query.get("q", [""])[0][:200],
                query.get("status", ["all"])[0],
                query.get("sort", ["name"])[0],
                query.get("direction", ["asc"])[0],
                query.get("game_status", []),
                query.get("engine", []),
                query.get("tag", []),
                query.get("personal_status", []),
                query.get("exclude_game_status", []),
                query.get("exclude_engine", []),
                query.get("exclude_tag", []),
                query.get("exclude_personal_status", []),
            ))
            return
        if parts.path == "/api/filters":
            self.send_json(get_filter_options())
            return
        if parts.path == "/api/catalog":
            query = parse_qs(parts.query)
            try:
                self.send_json(fetch_f95_catalog(query))
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 502)
            return
        if parts.path == "/api/catalog/details":
            query = parse_qs(parts.query)
            try:
                details = fetch_catalog_game_details(query.get("thread_id", [""])[0])
                self.send_json(details)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 502)
            return
        if parts.path == "/api/catalog/filters":
            try:
                query = parse_qs(parts.query)
                self.send_json(get_catalog_filter_options(force_refresh=query.get("refresh", [""])[0] == "1"))
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 502)
            return
        if parts.path == "/api/sync/status":
            self.send_json(get_sync_progress())
            return
        if parts.path == "/api/sync/reports":
            query = parse_qs(parts.query)
            after_id_value = query.get("after_id", ["0"])[0]
            if not after_id_value.isdigit():
                self.send_json({"error": "ID del riepilogo non valido."}, 400)
                return
            try:
                report_epoch = read_setting("sync_report_epoch")
                if query.get("epoch", [""])[0] != report_epoch:
                    after_id_value = "0"
                self.send_json(get_sync_reports(int(after_id_value)))
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 500)
            return
        if parts.path.startswith("/api/games/"):
            try:
                game_id = int(parts.path.rsplit("/", 1)[1])
            except ValueError:
                self.send_json({"error": "ID non valido."}, 400)
                return
            game = get_game(game_id)
            self.send_json(game if game else {"error": "Gioco non trovato."}, 200 if game else 404)
            return
        if parts.path == "/api/health":
            self.send_json({"ok": True})
            return
        if parts.path == "/api/dev/version" and DEV_MODE:
            self.send_json({"dev": True, "version": development_source_version()})
            return
        if parts.path == "/api/settings/auth":
            self.send_json(f95_session_status())
            return
        if parts.path == "/api/settings":
            self.send_json(get_sync_settings())
            return
        if parts.path.startswith("/api/"):
            self.send_json({"error": "Endpoint non trovato."}, 404)
            return
        if parts.path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self):
        parts = urlsplit(self.path)
        try:
            data = parse_json_body(self)
            if parts.path == "/api/settings/login":
                status = login_f95zone(data.get("username"), data.get("password"))
                self.send_json(status)
                return
            if parts.path == "/api/settings":
                settings = validate_sync_settings(data)
                with database() as db:
                    db.execute(
                        "INSERT OR REPLACE INTO settings (key, value) VALUES ('sync_mode', ?)",
                        (settings["sync_mode"],),
                    )
                    db.execute(
                        "INSERT OR REPLACE INTO settings (key, value) VALUES ('sync_interval', ?)",
                        (str(settings["sync_interval"]),),
                    )
                SETTINGS_CHANGED.set()
                self.send_json(settings)
                return
            if parts.path == "/api/games":
                thread_url = normalize_thread_url(data.get("thread_url"))
                fields = validate_game_fields({key: value for key, value in data.items() if key != "thread_url"}, creating=True)
                custom_title = fields.pop("title", "")
                title = custom_title or title_from_thread_url(thread_url)
                created_at = now_iso()
                user_version_updated_at = created_at if fields.get("user_version") else None
                with database() as db:
                    cursor = db.execute(
                        """
                        INSERT INTO games (thread_url, title, title_override, developer, user_version, user_version_updated_at,
                            game_status, engine, tags, personal_status, rating, notes, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            thread_url, title, int(bool(custom_title)), fields.get("developer", ""), fields.get("user_version", ""),
                            user_version_updated_at,
                            fields.get("game_status", ""), fields.get("engine", ""), fields.get("tags", "[]"),
                            fields.get("personal_status", "wishlist"), fields.get("rating"), fields.get("notes", ""),
                            created_at, created_at,
                        ),
                    )
                    game_id = cursor.lastrowid
                game = get_game(game_id)
                queue_sync_jobs([game_id], report_changes=False)
                self.send_json(game, 201)
                return
            if parts.path == "/api/sync":
                progress = queue_sync_jobs()
                self.send_json({"queued": progress["active"], "progress": progress}, 202)
                return
            match = re.fullmatch(r"/api/games/(\d+)/sync", parts.path)
            if match:
                game_id = int(match.group(1))
                if not get_game(game_id):
                    self.send_json({"error": "Gioco non trovato."}, 404)
                    return
                progress = queue_sync_jobs([game_id])
                self.send_json({"queued": progress["active"], "progress": progress}, 202)
                return
            self.send_json({"error": "Endpoint non trovato."}, 404)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, 400)
        except sqlite3.IntegrityError:
            self.send_json({"error": "Questo thread è già presente nella libreria."}, 409)

    def do_PATCH(self):
        match = re.fullmatch(r"/api/games/(\d+)", urlsplit(self.path).path)
        if not match:
            self.send_json({"error": "Endpoint non trovato."}, 404)
            return
        game_id = int(match.group(1))
        try:
            fields = validate_game_fields(parse_json_body(self))
            if not fields:
                raise ValueError("Nessun campo da aggiornare.")
            if not get_game(game_id):
                self.send_json({"error": "Gioco non trovato."}, 404)
                return
            self.send_json(update_game_fields(game_id, fields))
        except ValueError as exc:
            self.send_json({"error": str(exc)}, 400)

    def do_DELETE(self):
        path = urlsplit(self.path).path
        if path == "/api/settings/auth":
            logout_f95zone()
            self.send_json({"logged_in": False, "username": None})
            return
        if path == "/api/games":
            try:
                deleted = delete_games(parse_json_body(self).get("ids"))
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
                return
            self.send_json({"deleted": deleted})
            return
        match = re.fullmatch(r"/api/games/(\d+)", path)
        if not match:
            self.send_json({"error": "Endpoint non trovato."}, 404)
            return
        try:
            deleted = delete_games([int(match.group(1))])
        except ValueError:
            self.send_json({"error": "Gioco non trovato."}, 404)
            return
        self.send_json({"deleted": deleted == 1})


def main():
    initialize_db()
    threading.Thread(target=sync_worker, daemon=True).start()
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer((host, port), LibraryHandler)
    print(f"F95 Library disponibile sulla porta {port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
