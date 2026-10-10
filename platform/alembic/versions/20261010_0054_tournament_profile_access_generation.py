"""Add a durable generation for tournament profile-access projections.

The Redis projection is only a read optimization.  These row triggers make
every authorization-input mutation advance a PostgreSQL-owned generation in
the same transaction, including older application binaries and FK cascades.
"""

from __future__ import annotations

from alembic import op


revision = "20261010_0054"
down_revision = "20260913_0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE platform.tournaments
          ADD COLUMN profile_access_generation bigint NOT NULL DEFAULT 0,
          ADD CONSTRAINT ck_tournaments_profile_access_generation_nonnegative
            CHECK (profile_access_generation >= 0)
        """
    )
    op.execute(
        """
        CREATE FUNCTION platform.keep_tournament_profile_generation_monotonic()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, platform
        AS $function$
        BEGIN
          IF NEW.profile_access_generation IS DISTINCT FROM OLD.profile_access_generation THEN
            NEW.profile_access_generation := OLD.profile_access_generation + 1;
          END IF;
          RETURN NEW;
        END;
        $function$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_tournaments_profile_generation_monotonic
        BEFORE UPDATE OF profile_access_generation ON platform.tournaments
        FOR EACH ROW
        EXECUTE FUNCTION platform.keep_tournament_profile_generation_monotonic()
        """
    )
    op.execute(
        """
        CREATE FUNCTION platform.bump_tournament_profile_generation_from_child()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, platform
        AS $function$
        DECLARE
          affected_ids text[];
          affected_id text;
        BEGIN
          IF TG_OP = 'INSERT' THEN
            affected_ids := ARRAY[NEW.tournament_id];
          ELSIF TG_OP = 'DELETE' THEN
            affected_ids := ARRAY[OLD.tournament_id];
          ELSE
            IF TG_TABLE_NAME = 'tournament_participants' THEN
              IF OLD.tournament_id IS NOT DISTINCT FROM NEW.tournament_id
                 AND OLD.user_id IS NOT DISTINCT FROM NEW.user_id
                 AND OLD.status IS NOT DISTINCT FROM NEW.status THEN
                RETURN NEW;
              END IF;
            ELSE
              IF OLD.tournament_id IS NOT DISTINCT FROM NEW.tournament_id
                 AND OLD.user_id IS NOT DISTINCT FROM NEW.user_id THEN
                RETURN NEW;
              END IF;
            END IF;
            affected_ids := ARRAY[OLD.tournament_id, NEW.tournament_id];
          END IF;

          FOR affected_id IN
            SELECT DISTINCT candidate
            FROM unnest(affected_ids) AS ids(candidate)
            WHERE candidate IS NOT NULL
            ORDER BY candidate
          LOOP
            UPDATE platform.tournaments
            SET profile_access_generation = profile_access_generation + 1
            WHERE id = affected_id;
          END LOOP;

          IF TG_OP = 'DELETE' THEN
            RETURN OLD;
          END IF;
          RETURN NEW;
        END;
        $function$
        """
    )
    op.execute(
        """
        CREATE FUNCTION platform.bump_tournament_profile_generation_from_organizer()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, platform
        AS $function$
        BEGIN
          NEW.profile_access_generation := OLD.profile_access_generation + 1;
          RETURN NEW;
        END;
        $function$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_tournaments_profile_access_generation
        BEFORE UPDATE OF organizer_user_id ON platform.tournaments
        FOR EACH ROW
        WHEN (OLD.organizer_user_id IS DISTINCT FROM NEW.organizer_user_id)
        EXECUTE FUNCTION
          platform.bump_tournament_profile_generation_from_organizer()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_tournament_participants_profile_access_generation
        AFTER INSERT OR DELETE OR UPDATE OF tournament_id, user_id, status
        ON platform.tournament_participants
        FOR EACH ROW
        EXECUTE FUNCTION platform.bump_tournament_profile_generation_from_child()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_tournament_team_members_profile_access_generation
        AFTER INSERT OR DELETE OR UPDATE OF tournament_id, user_id
        ON platform.tournament_team_members
        FOR EACH ROW
        EXECUTE FUNCTION platform.bump_tournament_profile_generation_from_child()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_tournaments_profile_generation_monotonic "
        "ON platform.tournaments"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_tournament_team_members_profile_access_generation "
        "ON platform.tournament_team_members"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_tournament_participants_profile_access_generation "
        "ON platform.tournament_participants"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_tournaments_profile_access_generation "
        "ON platform.tournaments"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "platform.bump_tournament_profile_generation_from_child()"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "platform.bump_tournament_profile_generation_from_organizer()"
    )
    op.execute(
        "ALTER TABLE platform.tournaments "
        "DROP CONSTRAINT IF EXISTS ck_tournaments_profile_access_generation_nonnegative"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "platform.keep_tournament_profile_generation_monotonic()"
    )
    op.drop_column(
        "tournaments", "profile_access_generation", schema="platform"
    )
