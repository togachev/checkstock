import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from .database import Base


class OperationType(str, enum.Enum):
    RECEIPT = "RECEIPT"
    TRANSFER = "TRANSFER"
    FBS_RESERVE = "FBS_RESERVE"
    FBS_SHIP = "FBS_SHIP"
    FBO_SHIP = "FBO_SHIP"


class OperationStatus(str, enum.Enum):
    IN_TRANSIT = "IN_TRANSIT"
    COMPLETED = "COMPLETED"


class Warehouse(Base):
    __tablename__ = "warehouses"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    is_deleted = Column(Boolean, default=False)


class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, index=True)
    sku = Column(String, unique=True, index=True)
    name = Column(String)


class Stock(Base):
    __tablename__ = "stocks"
    id = Column(Integer, primary_key=True, index=True)
    warehouse_id = Column(Integer, ForeignKey("warehouses.id"))
    product_id = Column(Integer, ForeignKey("products.id"))
    physical_qty = Column(Integer, default=0)
    reserved_fbs_qty = Column(Integer, default=0)

    warehouse = relationship("Warehouse")
    product = relationship("Product")

    __table_args__ = (
        UniqueConstraint("warehouse_id", "product_id", name="_wh_prod_uc"),
    )


class Operation(Base):
    __tablename__ = "operations"
    id = Column(Integer, primary_key=True, index=True)
    operation_type = Column(Enum(OperationType))
    status = Column(Enum(OperationStatus))
    product_id = Column(Integer, ForeignKey("products.id"))
    qty = Column(Integer)
    from_wh_id = Column(Integer, ForeignKey("warehouses.id"), nullable=True)
    to_wh_id = Column(Integer, ForeignKey("warehouses.id"), nullable=True)
    idempotency_key = Column(
        String, unique=True, index=True
    )  # Защита от двойного клика
    created_at = Column(DateTime, default=datetime.utcnow)

    product = relationship("Product")
    from_wh = relationship("Warehouse", foreign_keys=[from_wh_id])
    to_wh = relationship("Warehouse", foreign_keys=[to_wh_id])
