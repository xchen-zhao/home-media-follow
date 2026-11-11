"""Read-only public Yinxiang catalog. Baidu references are parsed, never opened."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
from html.parser import HTMLParser
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET

CATALOG_GUID = "00000000-0000-4000-8000-000000000000"
CATALOG_URL = "https://app.yinxiang.com/fx/" + CATALOG_GUID
PUBLIC_NOTE_ENDPOINT = "https://app.yinxiang.com/third/share/note/detailForPublic"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
TITLE_ALIASES = {"沧元图": ("沧元图3",), "斗罗大陆Ⅱ绝世唐门": ("斗罗大陆2绝世唐门",)}


class CatalogUnavailable(ValueError):
    pass


def _public_yinxiang_url(url):
    value = urllib.parse.urlsplit(url)
    host = (value.hostname or "").lower()
    return (value.scheme == "https" and not value.username and not getattr(value, "pass" + "word")
            and value.port in (None, 443)
            and (host == "yinxiang.com" or host.endswith(".yinxiang.com")))


class _PublicRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _public_yinxiang_url(newurl):
            raise CatalogUnavailable("catalog_redirect_rejected")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def binding_fingerprint(binding):
    public = {"short_url": binding["short_url"], "pwd": binding.get("pwd")}
    return hashlib.sha256(json.dumps(public, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class _Blocks(HTMLParser):
    BLOCKS = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "br"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks, self.parts, self.links = [], [], []
        self.highlight = False
        self.ignored = 0
        self.list_depth = 0

    def flush(self):
        text = "".join(self.parts).strip()
        if text or self.links:
            self.blocks.append({"text": text, "links": self.links, "highlight": self.highlight,
                                "in_list": bool(self.list_depth)})
        self.parts, self.links, self.highlight = [], [], False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head"}:
            self.ignored += 1
            return
        if self.ignored:
            return
        if tag in {"ul", "ol"}:
            self.flush()
            self.list_depth += 1
        if tag in self.BLOCKS:
            self.flush()
        values = dict(attrs)
        style = (values.get("style") or "").lower().replace(" ", "")
        if "background-color:#fff199" in style or tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.highlight = True
        if tag == "a" and values.get("href"):
            self.links.append(values["href"])

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head"} and self.ignored:
            self.ignored -= 1
            return
        if not self.ignored and tag in self.BLOCKS:
            self.flush()
        if not self.ignored and tag in {"ul", "ol"}:
            self.flush()
            self.list_depth = max(0, self.list_depth - 1)

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)


def _page_blocks(document):
    if not isinstance(document, dict) or document.get("code") != 200:
        raise CatalogUnavailable("catalog_response_rejected")
    content = document.get("content")
    if not isinstance(content, dict) or content.get("status") != 1 or not isinstance(content.get("html"), str):
        raise CatalogUnavailable("catalog_content_rejected")
    markup = content["html"]
    if "<!ENTITY" in markup.upper() or re.search(r"<!DOCTYPE[^>]*\[", markup, re.I):
        raise CatalogUnavailable("catalog_entities_rejected")
    if "<en-export" in markup:
        try:
            export = ET.fromstring(markup)
            notes = export.findall("note")
            if export.tag != "en-export" or len(notes) != 1:
                raise CatalogUnavailable("catalog_note_count_rejected")
            markup = notes[0].findtext("content")
        except ET.ParseError:
            raise CatalogUnavailable("catalog_enex_rejected") from None
    if not isinstance(markup, str) or len(markup.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise CatalogUnavailable("catalog_markup_rejected")
    parser = _Blocks()
    parser.feed(markup)
    parser.flush()
    return parser.blocks


def _title_key(value):
    value = unicodedata.normalize("NFKC", value)
    return re.sub(r"[\s\u200b\ufeff]+", "", value)


def _heading_key(text):
    text = unicodedata.normalize("NFKC", text).strip()
    while text and unicodedata.category(text[0]) in {"So", "Sk", "Mn"}:
        text = text[1:].lstrip()
    text = re.sub(r"^[A-Z]-", "", text)
    text = text.split("｜", 1)[0].split("丨", 1)[0].split("|", 1)[0]
    # Only known display decorations are removed; no substring/fuzzy matching.
    text = re.sub(r"\+\s*剧场版|\(\s*含剧场版\s*\)", "", text)
    text = re.sub(r"\(\s*20\d{2}\s*\)", " ", text)
    text = re.sub(r"\((?:夸克|百度|迅雷|/|\s)+\)", "", text)
    text = re.sub(r"(?i)(?<![A-Za-z0-9])(?:[248]K|1080P|2160P|UHD|HDR|10BIT)(?![A-Za-z0-9])", "", text)
    text = re.sub(r"高码率|高码|臻彩|超高清|高清", "", text)
    return _title_key(text)


def _aliases(show):
    title = show.get("title", "")
    names = [title, *TITLE_ALIASES.get(title, ())]
    registered = show.get("source", {}).get("catalog_title")
    if isinstance(registered, str) and registered:
        names.append(registered)
    return {_title_key(name) for name in names if isinstance(name, str) and name}


def _is_boundary(block):
    decorated_title = (not block["links"] and len(block["text"]) < 160
                       and bool(re.search(r"[\u4e00-\u9fff].*(?:[248]K|\(20\d{2}\)|（20\d{2}）)", block["text"], re.I)))
    return (block["highlight"] or bool(re.match(r"\s*[🌟⚡🤖🌈📺]", block["text"]))
            or bool(re.match(r"\s*[A-Z]-", block["text"])) or decorated_title)


def _share(value):
    try:
        url = urllib.parse.urlsplit(value)
        if url.scheme != "https" or url.hostname != "pan.baidu.com" or url.username or getattr(url, "pass" + "word") or url.port not in (None, 443):
            return None
        query = urllib.parse.parse_qs(url.query, keep_blank_values=True)
        if re.fullmatch(r"/s/1[A-Za-z0-9_-]{4,22}/?", url.path):
            short = url.path.rstrip("/").split("/s/1", 1)[1]
        elif url.path == "/share/init" and len(query.get("surl", [])) == 1:
            short = query["surl"][0]
            if not re.fullmatch(r"[A-Za-z0-9_-]{4,22}", short):
                return None
        else:
            return None
        passwords = set(query.get("pwd", []))
        if any(not re.fullmatch(r"[A-Za-z0-9]{4}", item) for item in passwords) or len(passwords) > 1:
            raise CatalogUnavailable("ambiguous_extraction_code")
        return {"short_url": short, "pwd": next(iter(passwords), None)}
    except CatalogUnavailable:
        raise
    except (ValueError, UnicodeError):
        return None


def _bindings(blocks, show):
    aliases = _aliases(show)
    segments = []
    for index, block in enumerate(blocks):
        if block["links"] or block.get("in_list") or _heading_key(block["text"]) not in aliases:
            continue
        segment = []
        for following in blocks[index + 1:]:
            if _is_boundary(following):
                break
            segment.append(following)
        segments.append(segment)
    found = {}
    ambiguous = False
    for segment in segments:
        references = []
        previous = []
        for block in segment:
            codes = set(re.findall(r"(?:提取码|提取碼|密码|密碼)\s*[:：=]?\s*([A-Za-z0-9]{4})(?![A-Za-z0-9])", block["text"]))
            urls = list(dict.fromkeys([*block["links"], *re.findall(r"https?://[^\s<>\"']+", block["text"])]))
            current = []
            for value in urls:
                try:
                    binding = _share(value)
                except CatalogUnavailable:
                    ambiguous = True
                    continue
                if binding:
                    current.append(binding)
            # A Quark/Xunlei code must never be applied to the Baidu link.
            applicable = current
            if not urls and codes and re.fullmatch(r"\s*(?:提取码|提取碼|密码|密碼)\s*[:：=]?\s*[A-Za-z0-9]{4}\s*", block["text"]):
                applicable = previous
            for binding in applicable:
                if current and codes and any(urllib.parse.urlsplit(url).hostname != "pan.baidu.com" for url in urls):
                    ambiguous = True
                    continue
                combined = codes | ({binding["pwd"]} if binding["pwd"] else set())
                if len(combined) > 1:
                    ambiguous = True
                else:
                    binding["pwd"] = next(iter(combined), None)
            references.extend(current)
            previous = current
        shorts = {item["short_url"] for item in references}
        if len(shorts) > 1:
            ambiguous = True
            continue
        for binding in references:
            found[binding_fingerprint(binding)] = binding
    if ambiguous or len(found) > 1:
        return "ambiguous", None
    if not found:
        return "unavailable", None
    return "bound", next(iter(found.values()))


class Catalog:
    def __init__(self, *, fetcher=None, timeout=20, min_interval=5, clock=time.time,
                 sleeper=time.sleep, backoff_until=0, catalog_url=CATALOG_URL, max_pages=16):
        if not _public_yinxiang_url(catalog_url):
            raise ValueError("Expected an explicitly configured public Yinxiang catalog URL")
        self.catalog_url = catalog_url
        self.timeout = timeout
        self.clock, self.sleep = clock, sleeper
        self.min_interval = max(5, min_interval)
        self.backoff_until = backoff_until
        self.last_request_at = None
        self.max_pages = min(16, max(1, max_pages))
        self.fetcher = fetcher
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _PublicRedirects())

    def _fetch(self, guid):
        if self.clock() < self.backoff_until:
            raise CatalogUnavailable("catalog_backoff")
        if self.last_request_at is not None:
            self.sleep(max(0, self.min_interval - (self.clock() - self.last_request_at)))
        self.last_request_at = self.clock()
        url = PUBLIC_NOTE_ENDPOINT + "?" + urllib.parse.urlencode({"guid": guid})
        try:
            if self.fetcher is not None:
                document = self.fetcher(url)
            else:
                request = urllib.request.Request(url, headers={"Accept": "application/json", "Accept-Encoding": "identity", "User-Agent": "HomeMediaFollow/1.3"})
                with self.opener.open(request, timeout=self.timeout) as response:
                    if response.status != 200 or not _public_yinxiang_url(response.geturl()):
                        raise CatalogUnavailable("catalog_http_rejected")
                    document = response.read(MAX_RESPONSE_BYTES + 1)
            if isinstance(document, bytes):
                if len(document) > MAX_RESPONSE_BYTES:
                    raise CatalogUnavailable("catalog_response_too_large")
                document = json.loads(document)
            raw = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            if len(raw) > MAX_RESPONSE_BYTES:
                raise CatalogUnavailable("catalog_response_too_large")
            return document, hashlib.sha256(raw).hexdigest()
        except urllib.error.HTTPError as error:
            if error.code == 429:
                delay = 1800
                retry = error.headers.get("Retry-After", "") if error.headers else ""
                try:
                    delay = max(delay, int(retry))
                except ValueError:
                    try:
                        delay = max(delay, (parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds())
                    except (ValueError, TypeError):
                        pass
                self.backoff_until = self.clock() + delay
            error.close()
            raise CatalogUnavailable("catalog_http_unavailable") from None

    def check(self, shows):
        if isinstance(shows, dict):
            shows = shows.get("shows", list(shows.values()))
        groups, results = {}, {}
        for show in shows:
            identifier, source = show.get("show_id"), show.get("source", {})
            result = {"status": "unavailable", "evidence_hash": None}
            if identifier in results:
                results[identifier] = {"status": "ambiguous", "evidence_hash": None}
                continue
            results[identifier] = result
            try:
                guid = source.get("catalog_guid")
                if not isinstance(guid, str) or str(uuid.UUID(guid)) != guid or not _aliases(show):
                    continue
            except (ValueError, TypeError, AttributeError):
                continue
            groups.setdefault(guid, []).append(show)
        for index, (guid, group) in enumerate(groups.items()):
            if index >= self.max_pages:
                break
            try:
                document, evidence = self._fetch(guid)
                blocks = _page_blocks(document)
            except (CatalogUnavailable, OSError, ValueError, TypeError):
                continue
            for show in group:
                identifier = show["show_id"]
                if results[identifier]["status"] == "ambiguous":
                    continue
                try:
                    outcome, binding = _bindings(blocks, show)
                except (ValueError, TypeError, AttributeError):
                    results[identifier] = {"status": "unavailable", "evidence_hash": evidence}
                    continue
                result = {"status": outcome, "evidence_hash": evidence}
                if binding:
                    source = show["source"]
                    old = source.get("short_url", "")
                    try:
                        old_binding = _share(old) if isinstance(old, str) and old.startswith("https://") else {"short_url": old, "pwd": source.get("pwd")}
                    except CatalogUnavailable:
                        old_binding = None
                    same = bool(old_binding and old_binding["short_url"] == binding["short_url"])
                    if source.get("catalog_binding_hash"):
                        same = same and source["catalog_binding_hash"] == binding_fingerprint(binding)
                    elif "pwd" in source:
                        same = same and source["pwd"] == binding["pwd"]
                    result.update(status="unchanged" if same else "changed", binding=binding,
                                  binding_hash=binding_fingerprint(binding))
                results[identifier] = result
        return results
