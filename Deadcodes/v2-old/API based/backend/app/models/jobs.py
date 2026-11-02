from sqlalchemy import Boolean, Column, DateTime, Integer, Text, func
from .base import Base


class JobRun(Base):
    __tablename__ = "job_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task = Column(Text, nullable=False)
    trigger = Column(Text, nullable=False)
    started_at = Column(DateTime, nullable=False, server_default=func.now())
    finished_at = Column(DateTime)
    ok = Column(Boolean)
    msg = Column(Text)