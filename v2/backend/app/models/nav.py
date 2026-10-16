from sqlalchemy import (
    Column, Date, DateTime, Float, ForeignKey, Index, Integer,
    Text, UniqueConstraint, func,
)
from .base import Base


class NavScheme(Base):
    __tablename__ = "nav_schemes"

    scheme_code = Column(Integer, primary_key=True)
    isin_growth = Column(Text)
    scheme_name = Column(Text, nullable=False)
    fund_house = Column(Text, nullable=False)
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("idx_nav_schemes_name", "scheme_name"),
        Index("idx_nav_schemes_house", "fund_house"),
    )


class NavOption(Base):
    __tablename__ = "nav_options"

    id = Column(Integer, primary_key=True, autoincrement=True)
    scheme_code = Column(Integer, ForeignKey("nav_schemes.scheme_code", ondelete="CASCADE"), nullable=False)
    plan = Column(Text, nullable=False)
    option_name = Column(Text, nullable=False)
    isin_payout = Column(Text)
    isin_reinvest = Column(Text)

    __table_args__ = (
        UniqueConstraint("scheme_code", "plan", "option_name", name="uq_nav_options"),
        Index("idx_nav_options_scheme", "scheme_code"),
        Index("idx_nav_options_plan", "plan"),
    )


class NavHistory(Base):
    __tablename__ = "nav_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    nav_option_id = Column(Integer, ForeignKey("nav_options.id", ondelete="CASCADE"), nullable=False)
    nav_value = Column(Float, nullable=False)
    nav_date = Column(Date, nullable=False)
    imported_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("nav_option_id", "nav_date", name="uq_nav_history"),
        Index("idx_nav_history_date", "nav_date"),
        Index("idx_nav_history_option", "nav_option_id"),
    )