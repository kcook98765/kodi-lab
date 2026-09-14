"""SQLAlchemy mappings for the core target/managed-instance slice."""

from sqlalchemy import Boolean, CheckConstraint, Enum, ForeignKey, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from kodi_lab.domain import (
    ConnectionState,
    HealthStatus,
    InstanceRuntimeState,
    InstanceType,
    TargetAvailability,
    TargetType,
)
from kodi_lab.storage.metadata import Base


def _enum_values(enum_type: type[TargetType]) -> list[str]:
    return [item.value for item in enum_type]


class TargetRow(Base):
    """Persisted normalized target."""

    __tablename__ = "targets"
    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="target_name_not_blank"),
        CheckConstraint(
            "length(trim(kodi_version)) > 0", name="target_kodi_version_not_blank"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[TargetType] = mapped_column(
        Enum(
            TargetType,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="target_type",
        ),
        nullable=False,
    )
    kodi_version: Mapped[str] = mapped_column(Text, nullable=False)
    availability: Mapped[TargetAvailability] = mapped_column(
        Enum(
            TargetAvailability,
            values_callable=lambda enum: [item.value for item in enum],
            native_enum=False,
            create_constraint=True,
            name="target_availability",
        ),
        nullable=False,
    )
    health: Mapped[HealthStatus] = mapped_column(
        Enum(
            HealthStatus,
            values_callable=lambda enum: [item.value for item in enum],
            native_enum=False,
            create_constraint=True,
            name="health_status",
        ),
        nullable=False,
    )
    connection_state: Mapped[ConnectionState] = mapped_column(
        Enum(
            ConnectionState,
            values_callable=lambda enum: [item.value for item in enum],
            native_enum=False,
            create_constraint=True,
            name="connection_state",
        ),
        nullable=False,
    )


class ManagedInstanceRow(Base):
    """Managed-target subtype data with a shared stable identity."""

    __tablename__ = "managed_instances"
    __table_args__ = (
        CheckConstraint(
            "length(trim(runtime_revision)) > 0",
            name="managed_instance_runtime_revision_not_blank",
        ),
    )

    target_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("targets.id", ondelete="CASCADE"),
        primary_key=True,
    )
    instance_type: Mapped[InstanceType] = mapped_column(
        Enum(
            InstanceType,
            values_callable=lambda enum: [item.value for item in enum],
            native_enum=False,
            create_constraint=True,
            name="instance_type",
        ),
        nullable=False,
    )
    runtime_revision: Mapped[str] = mapped_column(Text, nullable=False)
    runtime_state: Mapped[InstanceRuntimeState] = mapped_column(
        Enum(
            InstanceRuntimeState,
            values_callable=lambda enum: [item.value for item in enum],
            native_enum=False,
            create_constraint=True,
            name="instance_runtime_state",
        ),
        nullable=False,
    )
    start_on_host_boot: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("0"),
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
