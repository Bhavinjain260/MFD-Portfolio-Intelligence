from sqlalchemy import Column, DateTime, Integer, Text, UniqueConstraint, func
from .base import Base


class TxnOverride(Base):
    __tablename__ = "txn_overrides"

    id = Column(Integer, primary_key=True, autoincrement=True)
    rta = Column(Text, nullable=False)
    folio = Column(Text, nullable=False)
    txn_no = Column(Text, nullable=False)
    fund = Column(Text, nullable=False, default="")
    status = Column(Text, nullable=False, default="inactive")
    reason = Column(Text)
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("rta", "folio", "txn_no", "fund", name="uq_txn_overrides"),
    )