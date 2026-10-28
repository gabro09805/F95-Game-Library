import json
import threading
import tempfile
import unittest
from http.cookiejar import Cookie
from pathlib import Path
from unittest.mock import patch

import app


class UrlValidationTests(unittest.TestCase):
    def test_accepts_f95_thread_url_and_drops_query(self):
        self.assertEqual(
            app.normalize_thread_url("https://f95zone.to/threads/a-game.12345/?foo=bar"),
            "https://f95zone.to/threads/a-game.12345/",
        )

    def test_rejects_non_thread_and_other_hosts(self):
        for value in (
            "https://example.com/threads/test.123/",
            "http://f95zone.to/threads/test.123/",
            "https://f95zone.to/forums/",
            "https://f95zone.to.evil.example/threads/test.123/",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                app.normalize_thread_url(value)

    def test_derives_title_from_thread_slug_without_numeric_id(self):
        self.assertEqual(
            app.title_from_thread_url("https://f95zone.to/threads/a-test-game.12345/"),
            "a test game",
        )

    def test_extracts_thread_id_from_slugged_and_numeric_thread_urls(self):
        self.assertEqual(
            app.thread_id_from_url("https://f95zone.to/threads/a-test-game.12345/"),
            12345,
        )
        self.assertEqual(
            app.thread_id_from_url("https://f95zone.to/threads/12345/"),
            12345,
        )


class MetadataParserTests(unittest.TestCase):
    def test_extracts_thread_metadata_and_latest_post_date(self):
        parser = app.ThreadMetadataParser()
        parser.feed(
            '<html><head><title>Old title | F95zone</title>'
            '<meta property="og:title" content="Game name | F95zone">'
            '<meta property="og:image" content="https://cdn.example/cover.jpg">'
            '<meta name="description" content="A game description"></head>'
            '<body><time datetime="2026-03-01T10:00:00Z"></time>'
            '<time datetime="2026-03-03T12:30:00Z"></time></body></html>'
        )
        self.assertEqual(app.clean_title(parser.meta["og:title"]), "Game name")
        self.assertEqual(parser.meta["og:image"], "https://cdn.example/cover.jpg")
        self.assertEqual(max(parser.post_times), "2026-03-03T12:30:00+00:00")

    def test_parses_unix_timestamp(self):
        self.assertEqual(app.parse_datetime("1772532000"), "2026-03-03T10:00:00+00:00")

    def test_parses_f95_update_date_labels(self):
        self.assertEqual(
            app.parse_datetime("October 26, 2026"),
            "2026-10-26T00:00:00+00:00",
        )
        self.assertEqual(
            app.parse_datetime("October 26, 2026 at 3:10 PM"),
            "2026-10-26T15:10:00+00:00",
        )

    def test_extracts_thread_page_title_tags_and_first_post_metadata(self):
        html = """
        <html><head>
          <title>Old title | F95zone</title>
          <meta property="og:title" content="Fallback title | F95zone">
          <meta property="og:image" content="https://cdn.example/fallback.jpg">
        </head><body>
          <h1 class="p-title-value">
            <span class="label pre-renpy">Ren'Py</span>
            Moonlight Story [v0.4] [Studio Name]
          </h1>
          <div class="js-tagList"><a class="tagItem">3D</a><a class="tagItem">Adventure</a></div>
          <article class="message--post message-threadStarterPost">
            <div class="message-body"><div class="bbWrapper">
              <p><b>Developer:</b> Sunny Studio</p>
              <p><b>Version:</b> 0.5</p>
              <p><b>Engine:</b> Unity</p>
              <p><b>Status:</b> Ongoing</p>
              <p><b>Overview:</b> The actual game overview.</p>
              <p><b>Thread Updated:</b> October 26, 2026</p>
              <p><b>Version:</b> 0.6</p>
              <div data-lb-single-image="1">
                <div class="lbContainer-zoomer" data-src="https://attachments.f95zone.to/data/cover.thumb.jpg"></div>
              </div>
              <img class="bbImage" src="https://attachments.f95zone.to/data/screenshot.jpg">
              <p>A story description.</p>
              <span class="u-hidden">Hidden content</span>
              <time datetime="2026-03-03T12:30:00Z"></time>
            </div></div>
          </article>
        </body></html>
        """

        result = app.parse_thread_metadata(html, "https://f95zone.to/threads/moonlight.123/")

        self.assertEqual(result["title"], "Moonlight Story")
        self.assertEqual(result["developer"], "Sunny Studio")
        self.assertEqual(result["version"], "0.5")
        self.assertEqual(result["engine"], "Unity")
        self.assertEqual(result["game_status"], "Ongoing")
        self.assertEqual(result["version"], "0.5")
        self.assertEqual(result["tags"], ["3D", "Adventure"])
        self.assertEqual(
            result["image_url"],
            "https://attachments.f95zone.to/data/cover.jpg",
        )
        self.assertEqual(
            result["gallery"],
            [
                "https://attachments.f95zone.to/data/cover.jpg",
                "https://attachments.f95zone.to/data/screenshot.jpg",
            ],
        )
        self.assertEqual(result["description"], "The actual game overview.")
        self.assertNotIn("Thread Updated", result["description"])
        self.assertNotIn("A story description.", result["description"])
        self.assertNotIn("Hidden content", result["description"])
        self.assertEqual(result["thread_updated_at"], "2026-10-26T00:00:00+00:00")

    def test_extracts_thread_likes_views_rating_and_status_prefix(self):
        html = """
        <html><body>
          <div class="p-title-meta"><dl><dt>Likes</dt><dd>1.2K</dd></dl></div>
          <div class="p-description"><ul class="listInline">Views: 23.4K</ul></div>
          <div class="ratingStarsRow" title="4.5 / 5"></div>
          <h1 class="p-title-value">
            <span class="label pre-ongoing">Ongoing</span>
            <span class="label pre-renpy">Ren'Py</span>
            Sample Game
          </h1>
          <article class="message--post message-threadStarterPost">
            <div class="message-body"><div class="bbWrapper">
              <p>Overview: A short description.</p>
            </div></div>
            <div data-reaction-count="7"></div>
          </article>
        </body></html>
        """
        result = app.parse_thread_metadata(html, "https://f95zone.to/threads/sample.789/")
        self.assertEqual(result["game_status"], "Ongoing")
        self.assertEqual(result["engine"], "Ren'Py")
        self.assertEqual(result["likes"], 1200)
        self.assertEqual(result["views"], 23400)
        self.assertEqual(result["f95_rating"], 4.5)

    def test_reads_rating_likes_and_views_from_f95_catalog_by_thread_id(self):
        response = json.dumps({
            "status": "ok",
            "msg": {
                "data": [
                    {"thread_id": 456, "rating": 4.7, "likes": 1234, "views": 56789},
                    {"thread_id": 789, "rating": 3.2, "likes": 99, "views": 111},
                ]
            },
        }).encode()
        with patch.object(app, "f95_request", return_value=(response, None)) as request:
            stats = app.fetch_catalog_stats(
                "Example Game",
                "https://f95zone.to/threads/example-game.456/",
            )

        self.assertEqual(stats, {"f95_rating": 4.7, "likes": 1234, "views": 56789})
        self.assertIn("latest_data.php?", request.call_args.args[0])
        self.assertIn("search=Example+Game", request.call_args.args[0])
        self.assertEqual(
            request.call_args.kwargs["headers"]["Referer"],
            "https://f95zone.to/sam/latest_alpha/",
        )

    def test_catalog_stats_rejects_malformed_payload_and_keeps_nonmatching_thread_distinct(self):
        with patch.object(app, "f95_request", return_value=(b"[]", None)):
            with self.assertRaisesRegex(ValueError, "ricerca nel catalogo"):
                app.fetch_catalog_stats("Example Game", "https://f95zone.to/threads/example.456/")

        response = json.dumps({
            "status": "ok",
            "msg": {"data": [{"thread_id": 789, "rating": 4.5, "likes": 1, "views": 2}]},
        }).encode()
        with patch.object(app, "f95_request", return_value=(response, None)):
            self.assertIsNone(
                app.fetch_catalog_stats("Example Game", "https://f95zone.to/threads/example.456/")
            )

    def test_overview_stops_at_thread_metadata_and_known_sections(self):
        text = (
            "Overview:\nAn overview with two paragraphs.\nSecond paragraph.\n"
            "Thread Updated: 2026-10-28\nVersion: 1.4\nDownloads:\nlinks"
        )
        self.assertEqual(
            app.extract_overview(text),
            "An overview with two paragraphs.\nSecond paragraph.",
        )

    def test_uses_heading_when_open_graph_title_is_missing(self):
        html = """
        <html><body>
          <h1 class="p-title-value">Unity Demo Game [1.2]</h1>
          <article class="message-threadStarterPost">
            <div class="message-body"><div class="bbWrapper">A description.</div></div>
          </article>
        </body></html>
        """
        result = app.parse_thread_metadata(html, "https://f95zone.to/threads/demo.456/")
        self.assertEqual(result["title"], "Demo Game")
        self.assertEqual(result["version"], "1.2")
        self.assertEqual(result["engine"], "Unity")
        self.assertEqual(result["description"], "A description.")


class ValidationTests(unittest.TestCase):
    def test_user_version_is_independent_editable_metadata(self):
        self.assertEqual(
            app.validate_game_fields({"user_version": "v1.2"}),
            {"user_version": "v1.2"},
        )

    def test_update_seen_flag_requires_boolean(self):
        self.assertEqual(app.validate_game_fields({"update_available": False}), {"update_available": 0})
        with self.assertRaises(ValueError):
            app.validate_game_fields({"update_available": 0})

    def test_image_proxy_accepts_only_https_f95zone_image_hosts(self):
        for value in (
            "https://attachments.f95zone.to/data/cover.jpg",
            "https://preview.f95zone.com/data/cover.jpg",
            "https://cdn.f95zone.ninja/data/cover.jpg",
        ):
            with self.subTest(value=value):
                host = app.urlsplit(value).hostname
                self.assertTrue(app.is_allowed_image_host(host))

        with self.assertRaises(ValueError):
            app.fetch_f95_image("https://example.com/image.jpg")
        with self.assertRaises(ValueError):
            app.fetch_f95_image("http://attachments.f95zone.to/image.jpg")


class GameListingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = app.DB_PATH
        app.DB_PATH = Path(self.temp_dir.name) / "library.sqlite3"
        app.initialize_db()
        games = [
            ("Alpha", "2026-01-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00", 3.0, 100, 1000),
            ("Bravo", "2026-01-03T00:00:00+00:00", "2026-02-03T00:00:00+00:00", 5.0, 50, 3000),
            ("Charlie", "2026-01-02T00:00:00+00:00", "2026-02-02T00:00:00+00:00", 4.0, 200, 2000),
        ]
        with app.database() as db:
            for name, created, updated, rating, likes, views in games:
                db.execute(
                    """
                    INSERT INTO games (
                        thread_url, title, created_at, updated_at, thread_updated_at,
                        f95_rating, likes, views
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"https://f95zone.to/threads/{name.lower()}.123/",
                        name,
                        created,
                        created,
                        updated,
                        rating,
                        likes,
                        views,
                    ),
                )

    def tearDown(self):
        app.DB_PATH = self.old_db_path
        self.temp_dir.cleanup()

    def test_sorts_all_supported_fields_and_directions(self):
        expectations = {
            ("name", "asc"): ["Alpha", "Bravo", "Charlie"],
            ("name", "desc"): ["Charlie", "Bravo", "Alpha"],
            ("updated", "desc"): ["Bravo", "Charlie", "Alpha"],
            ("added", "desc"): ["Bravo", "Charlie", "Alpha"],
            ("rating", "desc"): ["Bravo", "Charlie", "Alpha"],
            ("likes", "desc"): ["Charlie", "Alpha", "Bravo"],
            ("views", "desc"): ["Bravo", "Charlie", "Alpha"],
        }
        for (sort, direction), expected in expectations.items():
            with self.subTest(sort=sort, direction=direction):
                games = app.list_games(sort=sort, direction=direction)
                self.assertEqual([game["title"] for game in games], expected)

    def test_search_and_status_filters_preserve_sort(self):
        games = app.list_games(search="a", sort="name", direction="desc")
        self.assertEqual([game["title"] for game in games], ["Charlie", "Bravo", "Alpha"])

    def test_metadata_filters_combine_and_filter_options_list_available_values(self):
        with app.database() as db:
            db.execute(
                "UPDATE games SET game_status = ?, engine = ?, tags = ?, personal_status = ? WHERE title = ?",
                ("Ongoing", "Ren'Py", json.dumps(["Adventure", "Romance"]), "wishlist", "Alpha"),
            )
            db.execute(
                "UPDATE games SET game_status = ?, engine = ?, tags = ?, personal_status = ? WHERE title = ?",
                ("Completed", "Unity", json.dumps(["Action"]), "completed", "Bravo"),
            )

        games = app.list_games(
            game_statuses=["Ongoing"],
            engines=["Ren'Py"],
            tags=["Romance"],
            personal_statuses=["wishlist"],
        )
        self.assertEqual([game["title"] for game in games], ["Alpha"])
        self.assertEqual(
            app.get_filter_options(),
            {
                "game_statuses": ["Completed", "Ongoing"],
                "engines": ["Ren'Py", "Unity"],
                "tags": ["Action", "Adventure", "Romance"],
            },
        )

    def test_metadata_filters_can_exclude_values(self):
        with app.database() as db:
            db.execute(
                "UPDATE games SET game_status = ?, engine = ?, tags = ? WHERE title = ?",
                ("Ongoing", "Ren'Py", json.dumps(["Adventure"]), "Alpha"),
            )
            db.execute(
                "UPDATE games SET game_status = ?, engine = ?, tags = ? WHERE title = ?",
                ("Completed", "Unity", json.dumps(["Action"]), "Bravo"),
            )

        self.assertEqual(
            [game["title"] for game in app.list_games(exclude_game_statuses=["Ongoing"])],
            ["Bravo", "Charlie"],
        )
        self.assertEqual(
            [game["title"] for game in app.list_games(exclude_engines=["Unity"], exclude_tags=["Adventure"])],
            ["Charlie"],
        )
        self.assertEqual(
            [game["title"] for game in app.list_games(game_statuses=["Ongoing", "Completed"])],
            ["Alpha", "Bravo"],
        )

    def test_f95_catalog_maps_results_and_builds_reference_api_filters(self):
        response = json.dumps({
            "status": "ok",
            "msg": {
                "data": [{
                    "thread_id": 456,
                    "title": "Catalog Game",
                    "creator": "Studio",
                    "version": "0.5",
                    "rating": 4.5,
                    "likes": 123,
                    "views": 4567,
                    "cover": "https://attachments.f95zone.to/data/cover.jpg",
                    "ts": 1793180000,
                }],
                "pagination": {"page": 2, "total": 4},
                "count": 101,
                "prefixes": {"games": [{"name": "Engine", "prefixes": [{"id": 7, "name": "Ren'Py"}]}]},
                "tags": {"11": "Adventure", "12": "Romance"},
            },
        }).encode()
        query = {
            "search": ["Catalog Game"],
            "page": ["2"],
            "sort": ["likes"],
            "creator": ["Studio"],
            "ignored": ["show"],
            "date": ["365"],
            "prefixes[]": ["7"],
            "noprefixes[]": ["18"],
            "prefixtype": ["or"],
            "tags[]": ["11"],
            "notags[]": ["12"],
            "tagtype": ["and"],
        }
        with app.database() as db:
            db.execute(
                """
                INSERT INTO games (thread_url, title, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    "https://f95zone.to/threads/456/",
                    "Catalog Game",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
        with patch.object(app, "f95_request", return_value=(response, None)) as request:
            result = app.fetch_f95_catalog(query)

        self.assertEqual(result["total_games"], 101)
        self.assertEqual(result["page"], 2)
        self.assertEqual(result["total_pages"], 4)
        self.assertEqual(result["games"][0]["thread_url"], "https://f95zone.to/threads/456/")
        self.assertEqual(result["games"][0]["title"], "Catalog Game")
        self.assertEqual(result["games"][0]["rating"], 4.5)
        self.assertTrue(result["games"][0]["in_library"])
        self.assertEqual(result["filters"]["prefixes"], [{"id": 7, "name": "Ren'Py"}])
        self.assertEqual(
            result["filters"]["tags"],
            [{"id": 11, "name": "Adventure"}, {"id": 12, "name": "Romance"}],
        )
        request_query = app.parse_qs(app.urlsplit(request.call_args.args[0]).query)
        self.assertEqual(request_query["search"], ["Catalog Game"])
        self.assertEqual(request_query["creator"], ["Studio"])
        self.assertEqual(request_query["ignored"], ["show"])
        self.assertEqual(request_query["date"], ["365"])
        self.assertEqual(request_query["prefixes[]"], ["7"])
        self.assertEqual(request_query["noprefixes[]"], ["18"])
        self.assertEqual(request_query["tags[]"], ["11"])
        self.assertEqual(request_query["notags[]"], ["12"])
        self.assertEqual(request_query["prefixtype"], ["or"])
        self.assertEqual(request_query["tagtype"], ["and"])
        self.assertEqual(result["filters"]["engines"], [{"id": 7, "name": "Ren'Py"}])
        self.assertEqual(result["filters"]["statuses"], [])

    def test_f95_catalog_rejects_unsupported_sort_before_request(self):
        with patch.object(app, "f95_request") as request:
            with self.assertRaisesRegex(ValueError, "Ordinamento catalogo"):
                app.fetch_f95_catalog({"sort": ["unsupported"]})
        request.assert_not_called()

    def test_f95_catalog_validates_date_limit_and_tag_selection_limit(self):
        with patch.object(app, "f95_request") as request:
            with self.assertRaisesRegex(ValueError, "Limite data catalogo"):
                app.fetch_f95_catalog({"date": ["366"]})
            with self.assertRaisesRegex(ValueError, "massimo 10 tag"):
                app.fetch_f95_catalog({"tags[]": [str(tag) for tag in range(1, 12)]})
        request.assert_not_called()

    def test_f95_catalog_accepts_sam_date_slider_values(self):
        response = json.dumps({
            "status": "ok",
            "msg": {"data": [], "pagination": {"page": 1, "total": 1}, "count": 0},
        }).encode()
        with patch.object(app, "f95_request", return_value=(response, None)) as request:
            for days in (1, 3, 7, 14, 30, 90, 180, 365):
                app.fetch_f95_catalog({"date": [str(days)]})
                request_query = app.parse_qs(app.urlsplit(request.call_args.args[0]).query)
                self.assertEqual(request_query["date"], [str(days)])

    def test_catalog_game_details_fetches_one_validated_thread_on_demand(self):
        metadata = {
            "title": "Catalog Game",
            "version": "1.2",
            "description": "Overview",
            "gallery": ["https://attachments.f95zone.to/data/image.jpg"],
        }
        with patch.object(app, "fetch_thread_metadata", return_value=metadata) as fetch:
            result = app.fetch_catalog_game_details("456")
        fetch.assert_called_once_with("https://f95zone.to/threads/456/")
        self.assertEqual(result["title"], "Catalog Game")
        self.assertEqual(result["latest_version"], "1.2")
        self.assertEqual(result["thread_id"], 456)
        with self.assertRaisesRegex(ValueError, "ID del thread non valido"):
            app.fetch_catalog_game_details("../456")

    def test_catalog_filter_options_match_reference_embedded_json_and_html_options(self):
        source = """
        <script>
          const filters = {
            "prefixes": {"games": [{"name": "Engine", "prefixes": [{"id": 7, "name": "Ren'Py"}]}]},
            "tags": {"games": [{"id": 41, "name": "Adventure"}]}
          };
        </script>
        <select name="prefixes[]"><option value="18">Completed</option></select>
        <select name="tags[]"><option value="42">Romance</option></select>
        """
        options = app.catalog_filter_options_from_html(source)
        self.assertEqual(
            options["prefixes"],
            [{"id": 18, "name": "Completed"}, {"id": 7, "name": "Ren'Py"}],
        )
        self.assertEqual(
            options["tags"],
            [{"id": 41, "name": "Adventure"}, {"id": 42, "name": "Romance"}],
        )
        self.assertEqual(options["engines"], [{"id": 7, "name": "Ren'Py"}])
        self.assertEqual(options["statuses"], [])

    def test_catalog_filter_options_parse_sam_checkbox_labels_and_filter_groups(self):
        source = """
        <script>
          const filters = {
            "prefixes": {"games": [
              {"name": "Engine", "prefixes": [{"id": 44, "name": "ADRIFT"}]},
              {"name": "Status", "prefixes": [{"id": 45, "name": "WIP"}]},
              {"name": "PREFIX: OTHER", "prefixes": [{"id": 46, "name": "Collection"}]}
            ]},
            "tags": {"games": [{"id": 92, "name": "New SAM Tag"}]}
          };
        </script>
        <div><input type="checkbox" name="prefixes[]" id="prefix-44" value="44"><label for="prefix-44">ADRIFT</label></div>
        <div><input type="checkbox" name="prefixes[]" value="45"><label>WIP</label></div>
        <label><input type="checkbox" name="tags[]" id="tag-92" value="92">New SAM Tag</label>
        """
        options = app.catalog_filter_options_from_html(source)
        self.assertIn({"id": 44, "name": "ADRIFT"}, options["engines"])
        self.assertIn({"id": 46, "name": "Collection"}, options["others"])
        self.assertEqual(options["statuses"], [{"id": 45, "name": "WIP"}])
        self.assertIn({"id": 92, "name": "New SAM Tag"}, options["tags"])

    def test_catalog_prefix_filter_groups_match_sam_reference_values_and_order(self):
        names = [
            "ADRIFT", "Flash", "Godot", "HTML", "Java", "Others", "QSP", "RAGS",
            "RPGM", "Ren'Py", "Tads", "Unity", "Unreal Engine", "WebGL", "Wolf RPG",
        ]
        engine_prefixes = [
            {"id": item_id, "name": name} for item_id, name in enumerate(names, start=101)
        ]
        engine_prefixes.append({"id": 120, "name": "New SAM Engine"})
        source = {
            "games": [
                {"id": 3, "name": "Other", "prefixes": [
                    {"id": 19, "name": "Collection"},
                    {"id": 23, "name": "SiteRip"},
                    {"id": 13, "name": "VN"},
                ]},
                {"id": 1, "name": "Engine", "prefixes": engine_prefixes},
                {"id": 4, "name": "Status", "prefixes": [
                    {"id": 18, "name": "Completed"},
                    {"id": 20, "name": "Onhold"},
                    {"id": 22, "name": "Abandoned"},
                ]},
            ],
        }
        prefixes, groups = app.catalog_prefix_data(source)
        options = app.catalog_filter_options(prefixes, {}, groups)
        self.assertEqual(options["engines"], [
            {"id": item_id, "name": name}
            for item_id, name in enumerate(names, start=101)
        ] + [{"id": 120, "name": "New SAM Engine"}])
        self.assertEqual(options["others"], [
            {"id": 19, "name": "Collection"},
            {"id": 23, "name": "SiteRip"},
            {"id": 13, "name": "VN"},
        ])
        self.assertEqual(options["statuses"], [
            {"id": 22, "name": "Abandoned"},
            {"id": 18, "name": "Completed"},
            {"id": 20, "name": "Onhold"},
        ])

    def test_catalog_filter_options_explain_when_f95_login_is_required(self):
        app.CATALOG_FILTER_CACHE.update({"expires_at": 0, "options": None})
        login_page = b'<pre>Sorry, you have to be <a href="/login">logged in</a> to access this page</pre>'
        with (
            patch.object(app, "fetch_f95_catalog", return_value={"filters": {"prefixes": [], "tags": []}}),
            patch.object(app, "f95_request", return_value=(login_page, None)),
        ):
            with self.assertRaisesRegex(ValueError, "accedi a F95Zone dalle Impostazioni"):
                app.get_catalog_filter_options()

    def test_catalog_filter_refresh_reloads_current_sam_options(self):
        app.CATALOG_FILTER_CACHE.update({
            "expires_at": app.time.time() + 900,
            "options": {"prefixes": [], "engines": [], "statuses": [], "tags": []},
        })
        sam_page = b"""
        <script>
          const filters = {
            "prefixes": {"games": [
              {"name": "Status", "prefixes": [{"id": 18, "name": "Completed"}]},
              {"name": "Engine", "prefixes": [{"id": 7, "name": "Ren'Py"}]}
            ]},
            "tags": {"games": [{"id": 92, "name": "New SAM Tag"}]}
          };
        </script>
        """
        with patch.object(app, "f95_request", return_value=(sam_page, None)) as request:
            options = app.get_catalog_filter_options(force_refresh=True)
        request.assert_called_once()
        self.assertIn({"id": 92, "name": "New SAM Tag"}, options["tags"])
        self.assertIn({"id": 18, "name": "Completed"}, options["statuses"])
        self.assertIn({"id": 7, "name": "Ren'Py"}, options["engines"])

    def test_missing_f95_rating_sorts_as_zero_not_personal_rating(self):
        with app.database() as db:
            db.execute(
                """
                INSERT INTO games (
                    thread_url, title, created_at, updated_at, rating, f95_rating
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "https://f95zone.to/threads/no-rating.999/",
                    "No F95 rating",
                    "2026-01-04T00:00:00+00:00",
                    "2026-01-04T00:00:00+00:00",
                    5,
                    None,
                ),
            )

        games = app.list_games(sort="rating", direction="desc")
        self.assertEqual([game["title"] for game in games], ["Bravo", "Charlie", "Alpha", "No F95 rating"])


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = app.DB_PATH
        app.DB_PATH = Path(self.temp_dir.name) / "library.sqlite3"
        app.initialize_db()
        app.SYNC_QUEUE.clear()
        app.SYNC_PENDING_CHANGES.clear()
        app.SYNC_REPORT_GAME_IDS.clear()
        app.SYNC_CURRENT_ID = None
        app.SYNC_PROGRESS.update({
            "active": False,
            "completed": 0,
            "total": 0,
            "current_title": "",
            "errors": 0,
            "report_error": "",
        })
        with app.database() as db:
            db.execute(
                "INSERT INTO games (thread_url, title, created_at, updated_at, user_version_updated_at) VALUES (?, ?, ?, ?, ?)",
                (
                    "https://f95zone.to/threads/test.123/",
                    "Test",
                    app.now_iso(),
                    app.now_iso(),
                    None,
                ),
            )
            db.execute("UPDATE games SET user_version = '0.7' WHERE id = 1")

    def tearDown(self):
        app.DB_PATH = self.old_db_path
        self.temp_dir.cleanup()

    def test_sync_queue_reports_progress_for_each_game(self):
        started = threading.Event()
        continue_sync = threading.Event()

        def fake_sync(game_id):
            started.set()
            continue_sync.wait(2)
            return app.get_game(game_id)

        with patch.object(app, "sync_game", side_effect=fake_sync):
            progress = app.queue_sync_jobs([1])
            self.assertTrue(started.wait(1))
            self.assertTrue(progress["active"])
            self.assertEqual(progress["total"], 1)
            current = app.get_sync_progress()
            self.assertEqual(current["current_title"], "Test")
            self.assertEqual(current["completed"], 0)

            continue_sync.set()
            for _ in range(100):
                if not app.get_sync_progress()["active"]:
                    break
                threading.Event().wait(0.01)

        completed = app.get_sync_progress()
        self.assertFalse(completed["active"])
        self.assertEqual(completed["completed"], 1)
        self.assertEqual(completed["total"], 1)

    def test_user_version_date_changes_only_when_user_version_changes(self):
        with patch.object(app, "now_iso", return_value="2026-10-28T10:00:00+00:00"):
            updated = app.update_game_fields(1, {"user_version": "0.8"})
        self.assertEqual(updated["user_version_updated_at"], "2026-10-28T10:00:00+00:00")

        with patch.object(app, "now_iso", return_value="2026-10-28T11:00:00+00:00"):
            unchanged = app.update_game_fields(1, {"user_version": "0.8"})
        self.assertEqual(unchanged["user_version_updated_at"], "2026-10-28T10:00:00+00:00")

        cleared = app.update_game_fields(1, {"user_version": ""})
        self.assertIsNone(cleared["user_version_updated_at"])

    def test_sync_saves_gallery_and_metadata(self):
        metadata = {
            "title": "Test Game",
            "image_url": "https://attachments.f95zone.to/cover.jpg",
            "gallery": ["https://attachments.f95zone.to/cover.jpg"],
            "description": "Description",
            "developer": "Developer",
            "version": "1.0",
            "engine": "Unity",
            "game_status": "Completed",
            "tags": ["Adventure"],
            "thread_updated_at": None,
        }
        with patch.object(app, "fetch_thread_metadata", return_value=metadata):
            game = app.sync_game(1)

        self.assertEqual(game["gallery"], metadata["gallery"])
        self.assertEqual(game["image_url"], metadata["image_url"])
        self.assertEqual(game["developer"], "Developer")
        self.assertEqual(game["latest_version"], "1.0")
        self.assertEqual(game["user_version"], "0.7")
        self.assertEqual(game["f95_rating"], None)
        updated_metadata = {**metadata, "gallery": ["https://attachments.f95zone.to/new-shot.jpg"]}
        updated_metadata["version"] = "1.1"
        updated_metadata["likes"] = 24
        updated_metadata["views"] = 1200
        updated_metadata["f95_rating"] = 4.5
        with patch.object(app, "fetch_thread_metadata", return_value=updated_metadata):
            game = app.sync_game(1)
        self.assertEqual(game["gallery"], updated_metadata["gallery"])
        self.assertEqual(game["latest_version"], "1.1")
        self.assertEqual(game["likes"], 24)
        self.assertEqual(game["views"], 1200)
        self.assertEqual(game["f95_rating"], 4.5)

    def test_sync_reports_version_and_completion_status_changes(self):
        with app.database() as db:
            db.execute(
                """
                UPDATE games
                SET version = '1.0', latest_version = '1.0', game_status = 'Ongoing',
                    thread_updated_at = '2026-01-01T00:00:00+00:00'
                WHERE id = 1
                """
            )
        metadata = {
            "title": "Test",
            "image_url": "",
            "gallery": [],
            "description": "Description",
            "developer": "",
            "version": "1.1",
            "engine": "",
            "game_status": "Completed",
            "tags": [],
            "thread_updated_at": "2026-10-28T00:00:00+00:00",
        }
        with patch.object(app, "fetch_thread_metadata", return_value=metadata):
            progress = app.queue_sync_jobs([1])
            self.assertTrue(progress["active"])
            for _ in range(100):
                if not app.get_sync_progress()["active"]:
                    break
                threading.Event().wait(0.01)

        updated = app.get_game(1)
        self.assertEqual(updated["latest_version"], "1.1")
        self.assertEqual(updated["game_status"], "Completed")
        reports = app.get_sync_reports()
        self.assertEqual(len(reports["reports"]), 1)
        changes = reports["reports"][0]["games"][0]["changes"]
        self.assertIn(
            {"field": "latest_version", "from": "1.0", "to": "1.1"},
            changes,
        )
        self.assertIn(
            {"field": "game_status", "from": "Ongoing", "to": "Completed"},
            changes,
        )

    def test_initial_sync_after_adding_game_does_not_create_report(self):
        updated_game = app.get_game(1)
        updated_game["latest_version"] = "1.0"
        with patch.object(app, "sync_game", return_value=updated_game):
            app.queue_sync_jobs([1], report_changes=False)
            for _ in range(100):
                if not app.get_sync_progress()["active"]:
                    break
                threading.Event().wait(0.01)

        self.assertFalse(app.get_sync_progress()["active"])
        self.assertEqual(app.get_sync_reports()["reports"], [])

    def test_sync_reports_are_persisted_and_read_by_cursor(self):
        game = {
            "id": 1,
            "title": "Test",
            "thread_url": "https://f95zone.to/threads/test.123/",
            "image_url": "",
            "changes": [{"field": "latest_version", "from": "1.0", "to": "1.1"}],
        }
        app.save_sync_report([game])
        app.save_sync_report([game])

        first_page = app.get_sync_reports(limit=1)
        second_page = app.get_sync_reports(after_id=first_page["next_after_id"], limit=1)
        self.assertEqual(len(first_page["reports"]), 1)
        self.assertTrue(first_page["has_more"])
        self.assertEqual(len(second_page["reports"]), 1)
        self.assertFalse(second_page["has_more"])
        self.assertEqual(first_page["epoch"], second_page["epoch"])


class AppSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = app.DB_PATH
        app.DB_PATH = Path(self.temp_dir.name) / "library.sqlite3"
        app.initialize_db()
        self.addCleanup(self.cleanup_database)

    def cleanup_database(self):
        app.DB_PATH = self.old_db_path
        self.temp_dir.cleanup()

    def test_update_schedule_defaults_to_six_hours(self):
        self.assertEqual(
            app.get_sync_settings(),
            {"sync_mode": "hours", "sync_interval": 6},
        )
        self.assertEqual(
            app.sync_interval_seconds(app.get_sync_settings()),
            6 * 60 * 60,
        )

    def test_update_schedule_supports_manual_days_and_months(self):
        self.assertEqual(
            app.validate_sync_settings({"sync_mode": "manual", "sync_interval": ""}),
            {"sync_mode": "manual", "sync_interval": 6},
        )
        self.assertIsNone(
            app.sync_interval_seconds({"sync_mode": "manual", "sync_interval": 6})
        )
        self.assertEqual(
            app.sync_interval_seconds({"sync_mode": "days", "sync_interval": 2}),
            2 * 24 * 60 * 60,
        )
        self.assertEqual(
            app.sync_interval_seconds({"sync_mode": "months", "sync_interval": 1}),
            30 * 24 * 60 * 60,
        )

    def test_update_schedule_rejects_invalid_mode_and_interval(self):
        for settings in (
            {"sync_mode": "weeks", "sync_interval": 1},
            {"sync_mode": "hours", "sync_interval": 0},
            {"sync_mode": "days", "sync_interval": 10000},
            {"sync_mode": ["manual"], "sync_interval": 1},
        ):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                app.validate_sync_settings(settings)


class F95LoginTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = app.DB_PATH
        app.DB_PATH = Path(self.temp_dir.name) / "library.sqlite3"
        app.initialize_db()

    def tearDown(self):
        app.DB_PATH = self.old_db_path
        self.temp_dir.cleanup()

    def test_login_persists_session_cookie_but_not_password(self):
        def fake_request(url, data=None, headers=None, cookie_jar=None):
            if url.endswith("/login/"):
                return (
                    b'<input name="_xfToken" value="test-csrf-token">',
                    app.urlsplit(url),
                )
            cookie_jar.set_cookie(Cookie(
                version=0,
                name="xf_user",
                value="session-token",
                port=None,
                port_specified=False,
                domain=".f95zone.to",
                domain_specified=True,
                domain_initial_dot=True,
                path="/",
                path_specified=True,
                secure=True,
                expires=None,
                discard=True,
                comment=None,
                comment_url=None,
                rest={},
                rfc2109=False,
            ))
            return b"Welcome", app.urlsplit(url)

        with patch.object(app, "f95_request", side_effect=fake_request):
            result = app.login_f95zone("test-user", "test-only-password")

        self.assertEqual(result, {"logged_in": True, "username": "test-user"})
        self.assertNotIn("test-only-password", app.read_setting("f95_cookies"))
        self.assertEqual(app.f95_session_status()["username"], "test-user")
        app.logout_f95zone()
        self.assertFalse(app.f95_session_status()["logged_in"])

    def test_login_rejects_empty_credentials_without_network_request(self):
        with patch.object(app, "f95_request") as request:
            with self.assertRaises(ValueError):
                app.login_f95zone("", "")
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
