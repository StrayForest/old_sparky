"""Existing HTML reference live marker.

Revision ID: 20260513_0018
Revises: 20260507_0017
Create Date: 2026-05-13
"""

from __future__ import annotations


revision = "20260513_0018"
down_revision = "20260507_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """No-op marker for live deployments already stamped at this revision."""


def downgrade() -> None:
    """No-op marker for live deployments already stamped at this revision."""
