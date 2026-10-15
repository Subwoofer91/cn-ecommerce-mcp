from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


SERVER_PATH = Path(__file__).parents[1] / "server.py"
SPEC = importlib.util.spec_from_file_location("cn_ecommerce_server", SERVER_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def result_page(url: str, title: str, snippet: str) -> str:
    return f'''<ul class="result"><li class="res-list">
    <h3   class="res-title "><a href="https://www.so.com/link?x=1"
    data-mdurl="{url}">{title}</a></h3>
    <p class="res-desc">{snippet}</p>
    </li></ul>'''


class EcommerceMcpTests(unittest.TestCase):
    def test_default_links_cover_only_requested_three_platforms(self) -> None:
        payload = MODULE.build_search_links("静音 游戏鼠标")
        platforms = [item["platform"] for item in payload["links"]]
        self.assertEqual(platforms, ["pdd", "jd", "taobao"])
        self.assertFalse(payload["login_required"])

    def test_parse_three_platform_product_ids(self) -> None:
        fixtures = [
            (
                "https://mobile.yangkeduo.com/goods.html?goods_id=123456789",
                "pdd",
                "123456789",
            ),
            ("https://item.jd.com/3291007.html", "jd", "3291007"),
            (
                "https://item.taobao.com/item.htm?id=727025597212",
                "taobao",
                "727025597212",
            ),
        ]
        for url, platform, product_id in fixtures:
            with self.subTest(platform=platform):
                payload = MODULE.parse_product_link(url)
                self.assertTrue(payload["recognized"])
                self.assertEqual(payload["platform"], platform)
                self.assertEqual(payload["product_id"], product_id)

    def test_parses_taobao_price_discount_and_direct_link(self) -> None:
        page = result_page(
            "https://item.taobao.com/item.htm?id=727025597212",
            "英菲克 W9 游戏静音鼠标 - 淘宝网",
            "2026年9月10日 ￥56.9 登录查看更多优惠 快递免运费",
        )
        results = MODULE._parse_so_results(page, "taobao", "direct", 5)
        self.assertEqual(len(results), 1)
        item = results[0]
        self.assertEqual(item["displayed_price_cny"], 56.9)
        self.assertEqual(item["product_id"], "727025597212")
        self.assertEqual(item["link_type"], "direct_product")
        self.assertIn("包邮/免运费", item["general_promotions"])
        self.assertIn("登录后优惠详情", item["general_promotions"])

    def test_parses_pdd_public_promotion_price(self) -> None:
        page = result_page(
            "https://www.smzdm.com/p/136322406/",
            "英菲克 IN9 三模鼠标多少钱 - 什么值得买",
            (
                "2026年10月1日 拼多多 此款活动售价99元，"
                "领取满90元减10元优惠券，最终到手价88元。"
            ),
        )
        results = MODULE._parse_so_results(page, "pdd", "deal", 5, 150)
        self.assertEqual(len(results), 1)
        item = results[0]
        self.assertEqual(item["displayed_price_cny"], 88.0)
        self.assertIsNone(item["product_url"])
        self.assertEqual(item["link_type"], "promotion_source")
        self.assertTrue(item["matches_budget"])
        self.assertIn("满减/优惠券", item["general_promotions"])

    def test_deal_result_must_name_platform(self) -> None:
        page = result_page(
            "https://www.smzdm.com/p/136322406/",
            "英菲克 IN9 三模鼠标",
            "京东活动售价88元",
        )
        self.assertEqual(MODULE._parse_so_results(page, "pdd", "deal", 5), [])

    def test_budget_excludes_known_over_budget_result(self) -> None:
        page = result_page(
            "https://item.taobao.com/item.htm?id=1",
            "静音游戏鼠标 - 淘宝网",
            "￥226.29 登录查看更多优惠",
        )
        self.assertEqual(
            MODULE._parse_so_results(page, "taobao", "direct", 5, 150), []
        )

    def test_public_http_product_link_is_upgraded_to_https(self) -> None:
        page = result_page(
            "http://item.jd.com/3913810.html",
            "达尔优静音游戏鼠标 - 京东",
            "京东商品信息",
        )
        item = MODULE._parse_so_results(page, "jd", "direct", 5)[0]
        self.assertEqual(item["product_url"], "https://item.jd.com/3913810.html")

    def test_rejects_unsupported_platform(self) -> None:
        with self.assertRaises(ValueError):
            MODULE.build_search_links("鼠标", ["unknown"])


if __name__ == "__main__":
    unittest.main()
