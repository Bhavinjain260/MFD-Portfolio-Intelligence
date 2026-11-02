from sqlalchemy import Column, DateTime, Integer, Text, func
from .base import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(Text, unique=True, nullable=False, index=True)
    password_hash = Column(Text, nullable=False)
    is_admin = Column(Integer, default=0)
    created_at = Column(DateTime, server_default=func.now())
    last_login = Column(DateTime)