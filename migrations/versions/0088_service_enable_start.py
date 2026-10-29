"""Persist explicit service startup authority on copied-source enablement."""

import sqlalchemy as sa
from alembic import op

revision = "0088_service_enable_start"
down_revision = "0087_novnc_control"
branch_labels = None
depends_on = None

_TABLE = "development_source_enablement_operations"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            "service_start_authorized",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(
        sa.text(f"SELECT count(*) FROM {_TABLE} WHERE service_start_authorized != 0")
    ):
        raise RuntimeError("Service startup authority history cannot be discarded")
    op.drop_column(_TABLE, "service_start_authorized")
