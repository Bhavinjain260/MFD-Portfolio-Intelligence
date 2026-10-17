from sqlalchemy import (
    Column, DateTime, Float, Index, Integer, Text, UniqueConstraint, String, Boolean, Date
)
from .base import Base
from sqlalchemy.sql import func





class AdminSetting(Base):
    __tablename__ = "admin_settings"
    key = Column(Text, primary_key=True)
    value = Column(Text)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class SyncCredential(Base):
    __tablename__ = "sync_credentials"
    key = Column(Text, primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class AmcCodeMap(Base):
    __tablename__ = "amc_code_map"
    amc_code = Column(Text, primary_key=True)
    amc_name = Column(Text, nullable=False)


class MonthlyBrokerage(Base):
    __tablename__ = "monthly_brokerage"
    id = Column(Integer, primary_key=True, autoincrement=True)
    amc = Column(Text, nullable=False)
    month = Column(Text, nullable=False)
    year = Column(Integer, nullable=False)
    amount = Column(Float, nullable=False, default=0)
    notes = Column(Text)
    timestamp = Column(DateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("amc", "month", "year", name="ux_monthly_brokerage_amy"),
    )


class ClientFamily(Base):
    __tablename__ = "client_family"
    family_id = Column(Integer, primary_key=True, autoincrement=True)
    family_name = Column(Text, nullable=False)
    head_client_code = Column(Text, nullable=False)
    created_at = Column(DateTime, server_default=func.now())


class ClientFamilyMember(Base):
    __tablename__ = "client_family_member"
    family_id = Column(Integer, primary_key=True)
    client_code = Column(Text, primary_key=True)
    is_head = Column(Integer, default=0)
    added_at = Column(DateTime, server_default=func.now())


class SubBroker(Base):
    __tablename__ = "sub_brokers"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(Text, nullable=False)
    email = Column(Text)
    phone = Column(Text)
    role = Column(Text, nullable=False, default="Sub-Broker")
    sharing_pct = Column(Float, nullable=False, default=50.0)
    is_active = Column(Integer, default=1)
    notes = Column(Text)
    created_at = Column(DateTime, server_default=func.now())


class SubBrokerClient(Base):
    __tablename__ = "sub_broker_clients"
    sub_broker_id = Column(Integer, primary_key=True)
    client_code = Column(Text, primary_key=True, unique=True)
    assigned_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("idx_sbc_sub", "sub_broker_id"),
        Index("idx_sbc_client", "client_code"),
    )



class UploadHistory(Base):
    __tablename__ = "upload_history"
    
    id = Column(Integer, primary_key=True)
    kind = Column(String, nullable=False)
    filename = Column(String, nullable=False)
    size_bytes = Column(Integer, nullable=False)
    rows = Column(Integer, nullable=False, default=0)
    success = Column(Boolean, nullable=False)
    message = Column(Text)
    duration_ms = Column(Integer, nullable=False)
    created_at = Column(DateTime, server_default=func.now())

class SubBrokerPayout(Base):
    __tablename__ = "sub_broker_payouts"
    id = Column(Integer, primary_key=True, autoincrement=True)
    sub_broker_id = Column(Integer, nullable=False)
    period_type = Column(Text, nullable=False)
    period_key = Column(Text, nullable=False)
    brokerage_generated = Column(Float, nullable=False, default=0)
    share_amount = Column(Float, nullable=False, default=0)
    transferred_amount = Column(Float, nullable=False, default=0)
    transfer_date = Column(Text)
    transfer_reference = Column(Text)
    transfer_notes = Column(Text)
    status = Column(Text, nullable=False, default="pending")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("sub_broker_id", "period_type", "period_key", name="uq_sub_broker_payout"),
        Index("idx_sbp_sub", "sub_broker_id"),
        Index("idx_sbp_period", "period_type", "period_key"),
    )