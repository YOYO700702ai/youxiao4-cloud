"""Offline failure-path tests; never load app.py, environment, or live data."""
import copy
import unittest
from unittest.mock import Mock

import requests

from notion_scripts import NotionScripts, validate_script_info


DATABASE = "11111111-1111-1111-1111-111111111111"
PAGE = "22222222-2222-2222-2222-222222222222"
OTHER_PAGE = "33333333-3333-3333-3333-333333333333"


def response(status=200, data=None, headers=None):
    result = Mock(status_code=status, headers=headers or {})
    result.json.return_value = data
    result.text = "SERVICE-RAW-ERROR secret_test_token"
    return result


def page(name="測試劇本", **properties):
    return {"object": "page", "id": PAGE,
            "parent": {"type": "database_id", "database_id": DATABASE},
            "properties": {"劇本名稱": {"title": [{"plain_text": name}]}, **properties},
            "cover": None, "archived": False}


def query(*pages, has_more=False):
    return response(data={"results": list(pages), "has_more": has_more})


class NotionScriptsTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.sleeper = Mock()
        self.client = NotionScripts("secret_test_token", DATABASE,
                                   session=self.session, sleeper=self.sleeper)

    def requests(self, method=None, suffix=None):
        return [call for call in self.session.request.call_args_list
                if (method is None or call.args[0] == method)
                and (suffix is None or call.args[1].endswith(suffix))]

    def test_create_splits_long_rich_text_and_preserves_text(self):
        text = "甲" * 2001 + "🙂" * 1100
        self.session.request.side_effect = [query(), response(data={"id": PAGE})]
        ok, url = self.client.create({"名稱": "測試劇本", "簡介": text,
                                      "類型": "情感，推理", "人數": "5人、6人", "價格": "800"})
        self.assertTrue(ok)
        self.assertIn(PAGE.replace("-", ""), url)
        props = self.requests("POST", "/pages")[0].kwargs["json"]["properties"]
        chunks = props["劇情簡介"]["rich_text"]
        self.assertEqual("".join(item["text"]["content"] for item in chunks), text)
        self.assertTrue(all(len(item["text"]["content"].encode("utf-16-le")) // 2 <= 2000 for item in chunks))
        self.assertEqual(props["類型"]["multi_select"], [{"name": "情感"}, {"name": "推理"}])
        self.assertEqual(props["價格"]["number"], 800)
        for call in self.requests():
            self.assertEqual(call.kwargs["timeout"], (4, 12))
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertEqual(call.kwargs["headers"]["Notion-Version"], "2022-06-28")

    def test_duplicate_create_returns_page_url_without_write(self):
        self.session.request.return_value = query(page())
        ok, message = self.client.create({"名稱": "測試劇本"})
        self.assertFalse(ok)
        self.assertIn("已有同名", message)
        self.assertIn(PAGE.replace("-", ""), message)
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(self.requests()[0].kwargs["json"]["filter"]["title"], {"equals": "測試劇本"})

    def test_invalid_info_never_queries_or_silently_drops_fields(self):
        cases = [None, {}, {"名稱": ""}, {"名稱": " 測試"},
                 {"名稱": "測試", "未知": "value"}, {"名稱": "測試", "劇本名稱": "測試"},
                 {"名稱": "測試", "價格": 800.5}, {"名稱": "測試", "價格": True},
                 {"名稱": "測試", "價格": -1}, {"名稱": "測試", "價格": 100001},
                 {"名稱": "測試", "價格": "800元"}, {"名稱": "測試", "簡介": {}},
                 {"名稱": "測試", "人數": "4人"}, {"名稱": "測試", "類型": "玄幻"},
                 {"名稱": "測試", "類型": "推理//情感"}, {"名稱": "測試", "角色": ["甲", "甲"]},
                 {"名稱": "測試", "角色": ["甲，乙"]}, {"名稱": "測試", "簡介": "x" * 200001},
                 {"名稱": "測試", "簡介": "\ud800"}]
        for info in cases:
            with self.subTest(info_type=type(info).__name__):
                self.assertFalse(self.client.create(info)[0])
        self.session.request.assert_not_called()

    def test_normalization_is_idempotent_and_does_not_mutate_input(self):
        original = {"劇本名稱": "測試", "劇情簡介": None, "類型": "推理，情感", "價格": "0"}
        snapshot = copy.deepcopy(original)
        normalized = validate_script_info(original)
        self.assertEqual(original, snapshot)
        self.assertEqual(normalized, {"名稱": "測試", "簡介": None, "類型": ["推理", "情感"], "價格": 0})
        self.assertEqual(validate_script_info(normalized), normalized)

    def test_normalized_optional_empty_fields_can_be_created_and_updated(self):
        for blank in ("", None):
            with self.subTest(blank=blank):
                original = {"名稱": "測試劇本", "簡介": blank, "類型標籤": blank,
                            "時長": blank, "價格": None, "類型": blank,
                            "人數": blank, "角色": blank}
                normalized = validate_script_info(original)
                self.assertEqual(validate_script_info(normalized), normalized)
                self.assertEqual(normalized["角色"], [])
                self.session.request.reset_mock()
                self.session.request.side_effect = [query(), response(data={"id": PAGE}),
                                                    query(page()), response(data={"id": PAGE})]
                self.assertTrue(self.client.create(normalized)[0])
                self.assertTrue(self.client.update("測試劇本", normalized)[0])
                for call in self.requests("POST", "/pages") + self.requests("PATCH"):
                    props = call.kwargs["json"]["properties"]
                    for field in ("劇情簡介", "類型標籤", "時長"):
                        self.assertEqual(props[field], {"rich_text": []})
                    for field in ("類型", "人數", "角色"):
                        self.assertEqual(props[field], {"multi_select": []})
                    self.assertEqual(props["價格"], {"number": None})

    def test_multiple_or_paginated_matches_block_all_writes(self):
        for found in (query(page(), page()), query(page(), has_more=True), query(has_more=True)):
            self.session.request.return_value = found
            self.assertFalse(self.client.archive("測試劇本")[0])
        self.assertFalse(self.requests("PATCH"))

    def test_find_falls_back_to_contains_only_when_exact_empty(self):
        found = page("測試劇本完整版")
        self.session.request.side_effect = [query(), query(found)]
        result, error = self.client.find("測試劇本")
        self.assertIsNone(error)
        self.assertEqual(result, found)
        self.assertEqual(self.requests()[1].kwargs["json"]["filter"]["title"], {"contains": "測試劇本"})

    def test_query_retry_is_bounded_and_respects_retry_after(self):
        self.session.request.side_effect = [response(429, headers={"Retry-After": "2"}), query(page())]
        self.assertIsNone(self.client.find("測試劇本")[1])
        self.sleeper.assert_called_once_with(2)
        self.assertEqual(len(self.requests()), 2)

    def test_long_retry_after_does_not_retry_early(self):
        self.session.request.return_value = response(429, headers={"Retry-After": "60"})
        self.assertIsNone(self.client.find("測試劇本")[0])
        self.assertEqual(len(self.requests()), 1)
        self.sleeper.assert_not_called()

    def test_read_timeouts_retry_but_exhaustion_blocks_create(self):
        self.session.request.side_effect = requests.Timeout("secret_test_token")
        ok, message = self.client.create({"名稱": "測試劇本"})
        self.assertFalse(ok)
        self.assertNotIn("secret_test_token", message)
        self.assertEqual(len(self.requests()), 2)
        self.assertFalse(self.requests("POST", "/pages"))

    def test_create_timeout_matches_full_body_before_reporting_success(self):
        found = page(劇情簡介={"rich_text": [{"plain_text": "內容"}]}, 價格={"number": 800})
        found["cover"] = {"type": "external", "external": {"url": "https://example.com/cover.jpg"}}
        self.session.request.side_effect = [query(), requests.Timeout(), query(found)]
        ok, _ = self.client.create({"名稱": "測試劇本", "簡介": "內容", "價格": 800},
                                   "https://example.com/cover.jpg")
        self.assertTrue(ok)
        self.assertEqual(len(self.requests("POST", "/pages")), 1)

    def test_create_timeout_mismatch_does_not_claim_success_or_repeat_write(self):
        self.session.request.side_effect = [query(), requests.Timeout(), query(page()), query()]
        info = {"名稱": "測試劇本", "價格": 800}
        ok, message = self.client.create(info)
        self.assertFalse(ok)
        self.assertIn("尚未確認", message)
        self.assertFalse(self.client.create(info)[0])
        self.assertEqual(len(self.requests("POST", "/pages")), 1)

    def test_reconciliation_rejects_different_cover_or_parent(self):
        for field in ("cover", "parent"):
            found = page()
            found[field] = ({"type": "external", "external": {"url": "https://example.com/other.jpg"}}
                            if field == "cover" else {"database_id": OTHER_PAGE})
            self.session.request.side_effect = [query(), requests.Timeout(), query(found)]
            client = NotionScripts("fake", DATABASE, session=self.session, sleeper=self.sleeper)
            self.assertFalse(client.create({"名稱": "測試劇本"})[0])

    def test_create_server_failure_checks_result_without_retrying_post(self):
        self.session.request.side_effect = [query(), response(503), query(page())]
        self.assertTrue(self.client.create({"名稱": "測試劇本"})[0])
        self.assertEqual(len(self.requests("POST", "/pages")), 1)

    def test_malformed_create_success_requires_reconciliation(self):
        self.session.request.side_effect = [query(), response(data={}), query()]
        ok, message = self.client.create({"名稱": "測試劇本"})
        self.assertFalse(ok)
        self.assertIn("尚未確認", message)
        self.assertEqual(len(self.requests("POST", "/pages")), 1)

    def test_uncertain_create_rejects_changed_body_without_another_request(self):
        self.session.request.side_effect = [query(), requests.Timeout(), query()]
        self.assertFalse(self.client.create({"名稱": "測試劇本", "價格": 800})[0])
        calls_before = len(self.requests())
        self.assertFalse(self.client.create({"名稱": "測試劇本", "價格": 900})[0])
        self.assertEqual(len(self.requests()), calls_before)

    def test_reconciliation_does_not_accept_only_the_first_text_chunk(self):
        found = page(劇情簡介={"rich_text": [{"plain_text": "相同前段"}, {"plain_text": "不同後段"}]})
        self.session.request.side_effect = [query(), requests.Timeout(), query(found)]
        self.assertFalse(self.client.create({"名稱": "測試劇本", "簡介": "相同前段"})[0])

    def test_update_preserves_empty_clears_and_chinese_comma(self):
        self.session.request.side_effect = [query(page(劇情簡介={"rich_text": [{"plain_text": "舊"}]})), response(data={"id": PAGE})]
        self.assertTrue(self.client.update("測試劇本", {"簡介": "", "角色": [], "價格": None, "人數": "5人，6人"})[0])
        props = self.requests("PATCH")[0].kwargs["json"]["properties"]
        self.assertEqual(props["劇情簡介"], {"rich_text": []})
        self.assertEqual(props["角色"], {"multi_select": []})
        self.assertEqual(props["價格"], {"number": None})
        self.assertEqual(props["人數"], {"multi_select": [{"name": "5人"}, {"name": "6人"}]})

    def test_update_compares_all_text_fragments_not_only_first(self):
        self.session.request.return_value = query(page(劇情簡介={"rich_text": [{"plain_text": "第一"}, {"plain_text": "第二"}]}))
        ok, message = self.client.update("測試劇本", {"簡介": "第一第二"})
        self.assertTrue(ok)
        self.assertEqual(message, "目前已是指定資料，無需重複寫入")
        self.assertFalse(self.requests("PATCH"))

    def test_normalized_empty_fields_already_empty_are_a_successful_noop(self):
        info = validate_script_info({"名稱": "測試劇本", "簡介": "", "類型標籤": "",
                                     "時長": "", "價格": None, "類型": "", "人數": "", "角色": ""})
        found = page(劇情簡介={"rich_text": []}, 類型標籤={"rich_text": []},
                     時長={"rich_text": []}, 價格={"number": None},
                     類型={"multi_select": []}, 人數={"multi_select": []}, 角色={"multi_select": []})
        self.session.request.return_value = query(found)
        ok, message = self.client.update("測試劇本", info)
        self.assertTrue(ok)
        self.assertEqual(message, "目前已是指定資料，無需重複寫入")
        self.assertEqual(len(self.requests()), 1)
        self.assertFalse(self.requests("PATCH"))

    def test_invalid_patch_is_rejected_before_query(self):
        self.assertFalse(self.client.update("測試", {"價格": 1.9, "時長": "3小時"})[0])
        self.session.request.assert_not_called()

    def test_patch_timeout_is_not_retried(self):
        self.session.request.side_effect = [query(page()), requests.Timeout("secret_test_token")]
        ok, message = self.client.archive("測試劇本")
        self.assertFalse(ok)
        self.assertIn("尚未確認", message)
        self.assertNotIn("secret_test_token", message)
        self.assertEqual(len(self.requests("PATCH")), 1)

    def test_malformed_patch_success_is_not_claimed_as_success(self):
        self.session.request.side_effect = [query(page()), response(data={})]
        ok, message = self.client.archive("測試劇本")
        self.assertFalse(ok)
        self.assertIn("尚未確認", message)
        self.assertEqual(len(self.requests("PATCH")), 1)

    def test_non_json_response_is_sanitized_and_blocks_mutation(self):
        invalid = response()
        invalid.json.side_effect = ValueError("SERVICE-RAW-ERROR secret_test_token")
        self.session.request.return_value = invalid
        ok, message = self.client.archive("測試劇本")
        self.assertFalse(ok)
        self.assertNotIn("secret_test_token", message)
        self.assertFalse(self.requests("PATCH"))

    def test_encoded_total_payload_limit_rejects_without_network(self):
        info = {"名稱": "測試", "簡介": "甲" * 81000, "類型標籤": "乙" * 1000, "時長": "丙" * 1000}
        self.assertFalse(self.client.create(info)[0])
        self.session.request.assert_not_called()

    def test_write_429_is_not_retried(self):
        self.session.request.side_effect = [query(page()), response(429)]
        self.assertFalse(self.client.replace_cover("測試劇本", "https://example.com/a.jpg")[0])
        self.assertEqual(len(self.requests("PATCH")), 1)
        self.sleeper.assert_not_called()

    def test_errors_never_return_raw_service_body(self):
        self.session.request.return_value = response(403)
        _, message = self.client.find("測試劇本")
        self.assertNotIn("SERVICE-RAW-ERROR", message)
        self.assertNotIn("secret_test_token", message)

    def test_invalid_cover_rejected_before_query(self):
        for url in (None, "", "http://example.com/a.jpg", "https://user:secret@example.com/a.jpg", "https://example.com:invalid/a.jpg"):
            self.assertFalse(self.client.replace_cover("測試", url)[0])
        self.session.request.assert_not_called()

    def test_malformed_query_and_missing_configuration_fail_closed(self):
        self.session.request.return_value = response(data={"results": [page()]})
        self.assertFalse(self.client.archive("測試劇本")[0])
        client = NotionScripts("", DATABASE, session=self.session)
        self.assertFalse(client.create({"名稱": "測試"})[0])
        self.assertFalse(self.requests("PATCH"))

    def test_rename_rejects_existing_other_page(self):
        duplicate = page("新名稱")
        duplicate["id"] = OTHER_PAGE
        self.session.request.side_effect = [query(page()), query(duplicate)]
        self.assertFalse(self.client.update("測試劇本", {"名稱": "新名稱"})[0])
        self.assertFalse(self.requests("PATCH"))


if __name__ == "__main__":
    unittest.main()
