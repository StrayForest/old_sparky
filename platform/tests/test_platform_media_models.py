from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest

from python_packages.platform_infra.db import Base
from python_packages.platform_infra.models import MediaAsset, MediaVariant, PlayerProfile, Tournament


class MediaModelTests(unittest.TestCase):
    def test_expand_schema_retains_legacy_urls_and_adds_nullable_asset_links(self) -> None:
        self.assertIn("avatar_url", PlayerProfile.__table__.c)
        self.assertIn("banner_url", PlayerProfile.__table__.c)
        self.assertIn("cover_url", Tournament.__table__.c)
        self.assertTrue(PlayerProfile.__table__.c.avatar_asset_id.nullable)
        self.assertTrue(PlayerProfile.__table__.c.banner_asset_id.nullable)
        self.assertTrue(Tournament.__table__.c.banner_asset_id.nullable)

    def test_assets_have_database_purpose_status_and_ownership_checks(self) -> None:
        checks = {
            constraint.name: str(constraint.sqltext)
            for constraint in MediaAsset.__table__.constraints
            if constraint.__class__.__name__ == "CheckConstraint"
        }
        self.assertIn("ck_media_assets_purpose_allowed", checks)
        self.assertIn("ck_media_assets_status_allowed", checks)
        self.assertIn("ck_media_assets_ownership_matches_purpose", checks)
        self.assertIn("profile_avatar", checks["ck_media_assets_purpose_allowed"])
        self.assertIn("deleted", checks["ck_media_assets_status_allowed"])

    def test_variants_are_unique_immutable_webp_metadata(self) -> None:
        unique_columns = {
            tuple(column.name for column in constraint.columns)
            for constraint in MediaVariant.__table__.constraints
            if constraint.__class__.__name__ == "UniqueConstraint"
        }
        self.assertIn(("asset_id", "variant_name"), unique_columns)
        self.assertIn(("object_key",), unique_columns)
        self.assertNotIn("content", MediaVariant.__table__.c)

    def test_tournament_active_fk_breaks_metadata_cycle(self) -> None:
        foreign_key = next(iter(Tournament.__table__.c.banner_asset_id.foreign_keys))
        self.assertTrue(foreign_key.use_alter)
        self.assertEqual(foreign_key.ondelete, "SET NULL")
        self.assertGreater(len(Base.metadata.sorted_tables), 0)

    def test_migration_follows_current_head(self) -> None:
        migration_path = (
            Path(__file__).resolve().parents[1]
            / "alembic"
            / "versions"
            / "20260801_0036_media_assets.py"
        )
        spec = importlib.util.spec_from_file_location("media_migration_0036", migration_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.revision, "20260801_0036")
        self.assertEqual(module.down_revision, "20260731_0035")


if __name__ == "__main__":
    unittest.main()
