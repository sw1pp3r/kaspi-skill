from __future__ import annotations

import sys
import tempfile
import unittest
import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import kaspi  # noqa: E402


FIXTURES = Path(__file__).parent / "fixtures"


class DeliveryTests(unittest.TestCase):
    def test_delivery_enums_are_grouped(self) -> None:
        self.assertEqual(kaspi._delivery_info("EXPRESS")["deliveryGroup"], "today")
        self.assertEqual(kaspi._delivery_info("TOMORROW")["deliveryGroup"], "tomorrow")
        self.assertEqual(kaspi._delivery_info("TILL_2_DAYS")["deliveryGroup"], "later")
        self.assertEqual(kaspi._delivery_info(None)["deliveryGroup"], "unknown")

    def test_fast_window_keeps_today_and_tomorrow(self) -> None:
        items = [
            {"id": "a", "deliveryGroup": "today"},
            {"id": "b", "deliveryGroup": "tomorrow"},
            {"id": "c", "deliveryGroup": "later"},
            {"id": "d", "deliveryGroup": "unknown"},
        ]
        primary, later, unknown = kaspi._partition_by_delivery(items, "fast")
        self.assertEqual([item["id"] for item in primary], ["a", "b"])
        self.assertEqual([item["id"] for item in later], ["c"])
        self.assertEqual([item["id"] for item in unknown], ["d"])

    def test_today_window_does_not_silently_include_tomorrow(self) -> None:
        items = [
            {"id": "a", "deliveryGroup": "today"},
            {"id": "b", "deliveryGroup": "tomorrow"},
        ]
        primary, later, unknown = kaspi._partition_by_delivery(items, "today")
        self.assertEqual([item["id"] for item in primary], ["a"])
        self.assertEqual([item["id"] for item in later], ["b"])
        self.assertEqual(unknown, [])

    def test_exact_tomorrow_window_is_available(self) -> None:
        items = [
            {"id": "a", "deliveryGroup": "today"},
            {"id": "b", "deliveryGroup": "tomorrow"},
        ]
        primary, later, unknown = kaspi._partition_by_delivery(items, "tomorrow")
        self.assertEqual([item["id"] for item in primary], ["b"])
        self.assertEqual([item["id"] for item in later], ["a"])
        self.assertEqual(unknown, [])

    def test_any_window_keeps_unknown_delivery_separate(self) -> None:
        items = [
            {"id": "a", "deliveryGroup": "later"},
            {"id": "b", "deliveryGroup": "unknown"},
        ]
        primary, later, unknown = kaspi._partition_by_delivery(items, "any")
        self.assertEqual([item["id"] for item in primary], ["a"])
        self.assertEqual(later, [])
        self.assertEqual([item["id"] for item in unknown], ["b"])


class QrTests(unittest.TestCase):
    def test_search_result_keeps_qr_remote_url_internal(self) -> None:
        normalized = kaspi._normalize_item(
            {
                "id": "123",
                "title": "Товар",
                "shopLink": "/p/tovar-123/?c=750000000&utm_source=test",
                "deliveryDuration": "TOMORROW",
            },
            "товар",
            "750000000",
        )
        self.assertEqual(normalized["link"], "https://kaspi.kz/shop/p/tovar-123/?c=750000000")
        self.assertNotIn("qrCodeUrl", normalized)
        self.assertEqual(normalized["deliveryLabel"], "Завтра*")

    def test_lookalike_domain_is_rejected(self) -> None:
        self.assertIsNone(kaspi._public_product_url("https://evilkaspi.kz/p/123", "750000000"))

    def test_markdown_rows_materialize_local_png_for_codex(self) -> None:
        png = b"\x89PNG\r\n\x1a\nfixture"
        fetched_urls: list[str] = []

        def fetch(url: str, _timeout: float) -> bytes:
            fetched_urls.append(url)
            return png

        item = {
            "title": "Товар",
            "price": "1 000 ₸",
            "rating": 5,
            "reviews": 1,
            "deliveryLabel": "Завтра*",
            "link": "https://kaspi.kz/shop/p/tovar-123/?c=750000000",
        }
        with tempfile.TemporaryDirectory() as output_dir:
            rows = kaspi._markdown_rows(
                [item],
                qr_output_dir=output_dir,
                qr_fetcher=fetch,
            )
            local_path = Path(item["qrLocalPath"])
            self.assertTrue(local_path.is_absolute())
            self.assertEqual(local_path.read_bytes(), png)
            self.assertIn(f"![QR]({local_path})", rows[-1])
            self.assertNotIn("quickchart.io", rows[-1])
            self.assertEqual(len(fetched_urls), 1)
            self.assertIn("quickchart.io/qr", fetched_urls[0])
            self.assertNotIn("utm_source", fetched_urls[0])

    def test_official_qr_is_captured_from_kaspi_modal(self) -> None:
        calls: list[tuple[str, ...]] = []

        def browser(_session: str, *arguments: str, timeout: float) -> str:
            calls.append(arguments)
            if arguments[0] == "screenshot":
                Path(arguments[-1]).write_bytes(b"\x89PNG\r\n\x1a\nofficial")
            return "ok"

        item = {
            "link": "https://kaspi.kz/shop/p/tovar-123/?c=750000000",
            "cityCode": "750000000",
        }
        with tempfile.TemporaryDirectory() as output_dir:
            with mock.patch.object(
                kaspi,
                "_official_qr_target",
                return_value="https://l.kaspi.kz/shop/example",
            ), mock.patch.object(kaspi, "_run_agent_browser", side_effect=browser):
                path = kaspi._capture_official_qr(item, output_dir=output_dir)
            self.assertTrue(Path(path).is_file())
            self.assertEqual(item["qrKind"], "kaspi_official_app")
            self.assertEqual(item["qrTargetUrl"], "https://l.kaspi.kz/shop/example")
            self.assertEqual(item["qrEvidence"], "kaspi_product_modal")
            self.assertTrue(any(call[0] == "find" for call in calls))
            self.assertTrue(any(call[0] == "screenshot" for call in calls))


class RelevanceTests(unittest.TestCase):
    def setUp(self) -> None:
        payload = json.loads((FIXTURES / "search_lopatka.json").read_text())
        self.items = [
            kaspi._normalize_item(item, "лопатка кухонная деревянная бук", "750000000")
            for item in payload["data"]
        ]

    def test_material_and_category_spam_rank_below_real_spatulas(self) -> None:
        ranked, rejected = kaspi._rank_and_filter_items(
            self.items,
            ["лопатка кухонная деревянная бук"],
            required_terms=[],
            required_materials=["дерево|бук"],
            excluded_terms=[],
            min_relevance=25,
        )
        self.assertEqual(ranked[0]["id"], "110371831")
        self.assertNotIn("103136828", [item["id"] for item in ranked])
        self.assertIn("103136828", [item["id"] for item in rejected])
        self.assertIn("112150417", [item["id"] for item in rejected])

    def test_same_model_dedup_prefers_fast_listing(self) -> None:
        ranked, _ = kaspi._rank_and_filter_items(
            self.items,
            ["IKEA UTFORMA 60295023"],
            required_terms=[],
            required_materials=[],
            excluded_terms=[],
            min_relevance=20,
        )
        deduped = kaspi._dedupe_model_variants(ranked)
        utforma = next(item for item in deduped if "60295023" in item["canonicalModelKey"])
        self.assertEqual(utforma["id"], "110371831")
        self.assertEqual({variant["id"] for variant in utforma["listingVariants"]}, {"110371831", "163486551"})

    def test_verified_specs_merge_named_and_numeric_model_duplicates(self) -> None:
        base = {
            "title": "Лопатка IKEA бук, 1 шт",
            "brand": "IKEA",
            "categoryId": "03344",
            "specifications": [
                {"name": "Тип", "value": "лопатка"},
                {"name": "Материал", "value": "бук"},
                {"name": "Длина", "value": "34 см"},
            ],
            "relevanceScore": 70,
        }
        fast = dict(
            base,
            id="110371831",
            link="https://kaspi.kz/shop/p/ikea-602-950-23-110371831/",
            unitPrice=2289,
            deliveryGroup="today",
            deliveryLabel="Express, 23 июля до 15:00, 995 ₸*",
        )
        named = dict(
            base,
            id="163486551",
            link="https://kaspi.kz/shop/p/ikea-utforma-163486551/",
            unitPrice=2025,
            deliveryGroup="later",
            deliveryLabel="Доставка, 26 июля до 23:00, 995 ₸*",
        )
        deduped = kaspi._dedupe_enriched_items([named, fast])
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["id"], "110371831")
        self.assertEqual(
            {variant["id"] for variant in deduped[0]["listingVariants"]},
            {"110371831", "163486551"},
        )
        self.assertEqual(
            deduped[0]["dedupeEvidence"],
            "verified_brand_title_type_material_length",
        )


class LocationTests(unittest.TestCase):
    def test_saved_profile_supplies_cli_defaults_without_address(self) -> None:
        with tempfile.TemporaryDirectory() as output_dir:
            config_path = str(Path(output_dir) / "location.json")
            with mock.patch.dict("os.environ", {"KASPI_LOCATION_CONFIG": config_path}):
                set_args = kaspi.build_parser().parse_args(
                    [
                        "location",
                        "set",
                        "--city-code",
                        "750000000",
                        "--city-name",
                        "Алматы",
                        "--zone",
                        "Magnum_ZONE1",
                    ]
                )
                saved = kaspi.location(set_args)
                search_args = kaspi.build_parser().parse_args(
                    ["search", "--query", "лопатка"]
                )
                kaspi._resolve_location_args(search_args)
            self.assertFalse(saved["containsExactAddress"])
            self.assertEqual(search_args.city_code, "750000000")
            self.assertEqual(search_args.city_name, "Алматы")
            self.assertEqual(search_args.zone, "Magnum_ZONE1")
            self.assertNotIn("address", Path(config_path).read_text().lower())


class DetailsTests(unittest.TestCase):
    def test_product_context_and_conflicts_are_extracted(self) -> None:
        page = (FIXTURES / "product_conflict.html").read_text()
        product = kaspi._extract_json_ld(page)
        specs = kaspi._extract_specs(page)
        context = kaspi._extract_product_context(page, product)
        warnings = kaspi._detect_conflicts(product["description"], specs)
        self.assertEqual(context["categoryCodes"], ["Kitchen utensils", "Kitchenware"])
        self.assertIn("ST1", context["baseProductCodes"][0])
        self.assertTrue(any("Длина" in warning for warning in warnings))
        self.assertTrue(any("Материал" in warning for warning in warnings))

    def test_offer_normalization_has_seller_exact_date_and_evidence(self) -> None:
        payload = json.loads((FIXTURES / "offers.json").read_text())
        local_now = datetime.fromisoformat("2026-07-22T12:00:00+05:00")
        offers = kaspi._normalize_offers(payload, ZoneInfo("Asia/Almaty"), local_now)
        selected = kaspi._select_offer(offers, "fast")
        self.assertEqual(selected["merchantName"], "УЮТ")
        self.assertEqual(selected["deliveryCost"], 995.0)
        self.assertEqual(selected["deliveryDate"], "2026-07-23")
        self.assertEqual(selected["deliveryEvidence"], "seller_offers_api")
        self.assertEqual(selected["deliveryDateEvidence"], "seller_absolute_date")
        self.assertEqual(selected["deliveryGroup"], "tomorrow")
        self.assertIn("23 июля", selected["deliveryLabel"])

    def test_absolute_seller_date_overrides_stale_tomorrow_enum(self) -> None:
        payload = json.loads((FIXTURES / "offers.json").read_text())
        payload = deepcopy(payload)
        payload["offers"][0]["deliveryOptions"]["EXPRESS"]["delivery"] = (
            "2026-07-24T09:00:00.000+00:00"
        )
        local_now = datetime.fromisoformat("2026-07-22T12:00:00+05:00")
        offers = kaspi._normalize_offers(payload, ZoneInfo("Asia/Almaty"), local_now)
        self.assertEqual(offers[0]["deliveryDuration"], "TOMORROW")
        self.assertEqual(offers[0]["deliveryDate"], "2026-07-24")
        self.assertEqual(offers[0]["deliveryGroup"], "later")
        self.assertFalse(offers[0]["isFastDelivery"])

    def test_absolute_delivery_label_is_stable_near_midnight(self) -> None:
        instant = datetime.fromisoformat("2026-07-23T00:30:00+05:00")
        label = kaspi._format_delivery_datetime(instant, "EXPRESS", 995.0)
        self.assertIn("23 июля", label)
        self.assertIn("00:30", label)


if __name__ == "__main__":
    unittest.main()
