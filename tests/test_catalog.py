import json
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
import unittest
import uuid

from home_media_follow.catalog import (
    CATALOG_URL, MAX_RESPONSE_BYTES, Catalog, CatalogUnavailable,
    _PublicRedirects, binding_fingerprint,
)

GUID = "00000000-0000-4000-8000-000000000001"


def show(title="凡人修仙传", identifier="fanren", short="oldShare", guid=GUID, **source):
    return {"show_id": identifier, "title": title,
            "source": {"catalog_guid": guid, "short_url": short, **source}}


def document(markup, enex=False):
    if enex:
        markup = ('<?xml version="1.0"?><!DOCTYPE en-export SYSTEM "http://xml.evernote.com/pub/evernote-export4.dtd">'
                  '<en-export><note><content><![CDATA[' + markup + ']]></content></note></en-export>')
    return {"code": 200, "content": {"status": 1, "html": markup}}


def title(text):
    return '<p>🌟<span style="background-color:#fff199">' + text + '</span></p>'


def share(short="newShare", code="A1b2"):
    query = "?pwd=" + code if code is not None else ""
    return '<div>BD:<a href="https://pan.baidu.com/s/1' + short + query + '">百度</a></div>'


def catalog(doc):
    return Catalog(fetcher=lambda _: doc, sleeper=lambda _: None)


class CatalogTests(unittest.TestCase):
    def test_exact_title_segment_not_first_intro_mention(self):
        intro = '<p>精品动漫：凡人修仙传｜光阴之外（下滑自取）</p>'
        markup = intro + title("凡人修仙传 4K 高码｜11点05") + share("fanrenShare")
        markup += title("光阴之外（2025）4K｜10点05") + share("guangyinShare")
        result = catalog(document(markup, enex=True)).check([
            show(), show("光阴之外", "guangyin"),
        ])
        self.assertEqual(result["fanren"]["binding"]["short_url"], "fanrenShare")
        self.assertEqual(result["guangyin"]["binding"]["short_url"], "guangyinShare")
        self.assertEqual(len(result["fanren"]["evidence_hash"]), 64)

    def test_intro_only_never_borrows_another_title_link(self):
        markup = '<p>精品动漫：凡人修仙传｜光阴之外</p>' + title("光阴之外（2025）4K") + share()
        result = catalog(document(markup)).check([show()])["fanren"]
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("binding", result)

    def test_list_items_cannot_be_resource_headings(self):
        markup = '<ul><li>凡人修仙传</li></ul>' + share()
        self.assertEqual(catalog(document(markup)).check([show()])["fanren"]["status"], "unavailable")

    def test_unknown_next_resource_title_stops_link_capture(self):
        markup = title("凡人修仙传 4K") + '<p>暂缺</p>' + title("其他作品 4K") + share()
        self.assertEqual(catalog(document(markup)).check([show()])["fanren"]["status"], "unavailable")
        markup = title("凡人修仙传 4K") + '<p>其他作品 4K</p>' + share()
        self.assertEqual(catalog(document(markup)).check([show()])["fanren"]["status"], "unavailable")

    def test_theatrical_title_is_not_the_regular_series(self):
        markup = title("遮天剧场版 背棺战王腾（2026）4K") + share()
        result = catalog(document(markup)).check([show("遮天", "zhetian")])
        self.assertEqual(result["zhetian"]["status"], "unavailable")

    def test_multiple_baidu_shares_are_ambiguous(self):
        markup = title("凡人修仙传 4K") + share("firstShare") + share("secondShare")
        self.assertEqual(catalog(document(markup)).check([show()])["fanren"]["status"], "ambiguous")

    def test_distinct_repeated_title_bindings_are_ambiguous(self):
        markup = title("凡人修仙传 4K") + share("firstShare") + title("凡人修仙传 4K") + share("secondShare")
        self.assertEqual(catalog(document(markup)).check([show()])["fanren"]["status"], "ambiguous")

    def test_quark_code_is_not_used_for_baidu(self):
        markup = title("凡人修仙传 4K")
        markup += '<div>KK:<a href="https://pan.quark.cn/s/example">夸克</a> 提取码：Q9w8</div>'
        markup += share(code="B2a3")
        result = catalog(document(markup)).check([show()])["fanren"]
        self.assertEqual(result["binding"]["pwd"], "B2a3")

    def test_adjacent_standalone_code_binds_only_previous_baidu_line(self):
        markup = title("凡人修仙传 4K") + share(code=None) + '<p>提取码：B2a3</p>'
        result = catalog(document(markup)).check([show()])["fanren"]
        self.assertEqual(result["binding"]["pwd"], "B2a3")

    def test_conflicting_baidu_codes_are_ambiguous(self):
        markup = title("凡人修仙传 4K") + share(code="A1b2") + '<p>提取码：B2a3</p>'
        self.assertEqual(catalog(document(markup)).check([show()])["fanren"]["status"], "ambiguous")
        markup = title("凡人修仙传 4K") + '<p><a href="https://pan.baidu.com/s/1newShare?pwd=A1b2&amp;pwd=B2a3">百度</a></p>'
        self.assertEqual(catalog(document(markup)).check([show()])["fanren"]["status"], "ambiguous")

    def test_unchanged_changed_and_code_rotation_hash(self):
        binding = {"short_url": "newShare", "pwd": "A1b2"}
        doc = document(title("凡人修仙传 4K") + share())
        result = catalog(doc).check([show(short="newShare", catalog_binding_hash=binding_fingerprint(binding))])["fanren"]
        self.assertEqual(result["status"], "unchanged")
        changed = catalog(doc).check([show(short="newShare", catalog_binding_hash=binding_fingerprint({**binding, "pwd": "Z9z9"}))])["fanren"]
        self.assertEqual(changed["status"], "changed")
        self.assertEqual(catalog(doc).check([show(short="oldShare")])["fanren"]["status"], "changed")

    def test_missing_code_share_remains_valid_candidate(self):
        result = catalog(document(title("凡人修仙传 4K") + share(code=None))).check([show()])["fanren"]
        self.assertIsNone(result["binding"]["pwd"])

    def test_only_public_yinxiang_endpoint_fetched_never_share(self):
        calls = []
        def fetch(url):
            calls.append(url)
            self.assertEqual(urllib.parse.urlsplit(url).hostname, "app.yinxiang.com")
            self.assertIn("/third/share/note/detailForPublic?guid=", url)
            return document(title("凡人修仙传 4K") + share())
        result = Catalog(fetcher=fetch, sleeper=lambda _: None).check([show()])
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["fanren"]["status"], "changed")
        with self.assertRaises(ValueError):
            Catalog(catalog_url="https://other.example/")
        self.assertEqual(Catalog().catalog_url, CATALOG_URL)

    def test_redirects_to_other_domains_or_baidu_are_rejected(self):
        handler = _PublicRedirects()
        request = urllib.request.Request(CATALOG_URL)
        for target in ["http://app.yinxiang.com/", "https://127.0.0.1/", "https://pan.baidu.com/s/1share", "https://yinxiang.com.evil.example/"]:
            with self.subTest(target=target), self.assertRaises(CatalogUnavailable):
                handler.redirect_request(request, None, 302, "", {}, target)

    def test_429_pauses_all_source_requests_and_exposes_backoff(self):
        now, calls = [1000], []
        def fetch(url):
            calls.append(url)
            raise urllib.error.HTTPError(url, 429, "limited", {"Retry-After": "3600"}, None)
        worker = Catalog(fetcher=fetch, clock=lambda: now[0], sleeper=lambda _: None)
        shows = [show(), show("光阴之外", "other", guid=str(uuid.uuid4()))]
        result = worker.check(shows)
        self.assertEqual(len(calls), 1)
        self.assertEqual(worker.backoff_until, 4600)
        self.assertTrue(all(item["status"] == "unavailable" for item in result.values()))
        worker.check(shows)
        self.assertEqual(len(calls), 1)

    def test_grouped_pages_budget_and_interval(self):
        calls, sleeps, now = [], [], [100]
        def sleep(seconds):
            sleeps.append(seconds); now[0] += seconds
        def fetch(url):
            calls.append(url)
            return document(title("凡人修仙传 4K") + share())
        shows = [show(), show("光阴之外", "same"), show("其他", "another", guid=str(uuid.uuid4()))]
        Catalog(fetcher=fetch, clock=lambda: now[0], sleeper=sleep).check(shows)
        self.assertEqual(len(calls), 2)
        self.assertGreaterEqual(sum(sleeps), 5)
        calls.clear()
        Catalog(fetcher=fetch, sleeper=lambda _: None, max_pages=1).check(shows)
        self.assertEqual(len(calls), 1)

    def test_bad_structure_oversize_and_entities_do_not_reuse_old_binding(self):
        bad = [b"x" * (MAX_RESPONSE_BYTES + 1), {"code": 200, "content": {"status": 0}},
               document('<!DOCTYPE en-export [<!ENTITY x "abc">]><en-export/>')]
        for doc in bad:
            result = catalog(doc).check([show()])["fanren"]
            self.assertEqual(result["status"], "unavailable")
            self.assertNotIn("binding", result)

    def test_cached_real_thirteen_bindings_seven_pages(self):
        root = Path(__file__).resolve().parents[1] / "operations"
        base, extra = root / "2026-11-07-xiafanju-rebuild", root / "2026-11-07-quality-fallback"
        if not (base / "source-bindings.json").exists():
            self.skipTest("Public snapshot fixtures not shipped to this environment")
        rows = json.loads((base / "source-bindings.json").read_text()) + json.loads((extra / "extra-source-bindings.json").read_text())
        shows = [{"show_id": "registered_" + str(i), "title": row["title"], "source": row} for i, row in enumerate(rows)]
        calls = []
        def fetch(url):
            guid = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["guid"][0]
            calls.append(guid)
            path = next(root.glob("*/catalog-" + guid + ".json"))
            return json.loads(path.read_text())
        result = Catalog(fetcher=fetch, sleeper=lambda _: None).check(shows)
        self.assertEqual(len(result), 13)
        self.assertEqual(len(calls), 7)
        for show in shows:
            row = result[show["show_id"]]
            self.assertTrue(row["status"] == "unchanged", show["title"] + ": " + row["status"])
            self.assertTrue(row["binding"]["short_url"] == show["source"]["short_url"], show["title"])
            self.assertTrue(row["binding"]["pwd"] == show["source"]["pwd"], show["title"])


if __name__ == "__main__":
    unittest.main()
