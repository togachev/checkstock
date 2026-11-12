import uuid
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import database, models

app = FastAPI(title="CheckStock")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return HTMLResponse(
        f"<script>alert('{exc.detail}'); window.location.href = '/';</script>",
        status_code=exc.status_code,
    )


def seed_data(db: Session):
    if db.query(models.Warehouse).count() > 0:
        return

    w_north = models.Warehouse(name="Север")
    w_south = models.Warehouse(name="Юг")
    db.add_all([w_north, w_south])
    db.commit()

    p1 = models.Product(sku="SKU-101", name="Органайзер")
    p2 = models.Product(sku="SKU-102", name="Контейнер")
    p3 = models.Product(sku="SKU-103", name="Корзина")
    db.add_all([p1, p2, p3])
    db.commit()

    stocks = [
        models.Stock(warehouse_id=w_north.id, product_id=p1.id, physical_qty=10),
        models.Stock(warehouse_id=w_south.id, product_id=p1.id, physical_qty=6),
        models.Stock(warehouse_id=w_north.id, product_id=p2.id, physical_qty=5),
        models.Stock(warehouse_id=w_south.id, product_id=p3.id, physical_qty=4),
    ]
    db.add_all(stocks)
    db.commit()


@app.on_event("startup")
def startup():
    models.Base.metadata.create_all(bind=database.engine)
    db = database.SessionLocal()
    seed_data(db)
    db.close()


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(database.get_db)):
    warehouses = db.query(models.Warehouse).all()
    products = db.query(models.Product).all()
    active_warehouses = [w for w in warehouses if not w.is_deleted]
    stocks = db.query(models.Stock).all()
    in_transit = (
        db.query(models.Operation)
        .filter(models.Operation.status == models.OperationStatus.IN_TRANSIT)
        .order_by(models.Operation.created_at.desc())
        .all()
    )
    history = (
        db.query(models.Operation)
        .filter(models.Operation.status == models.OperationStatus.COMPLETED)
        .order_by(models.Operation.created_at.desc())
        .limit(20)
        .all()
    )

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "stocks": stocks,
            "warehouses": warehouses,
            "active_warehouses": active_warehouses,
            "products": products,
            "in_transit": in_transit,
            "history": history,
        },
    )


@app.post("/warehouse/add")
def add_warehouse(name: str = Form(...), db: Session = Depends(database.get_db)):
    db.add(models.Warehouse(name=name))
    db.commit()
    return RedirectResponse("/", status_code=303)


# НОВОЕ: Переименование склада
@app.post("/warehouse/rename/{wh_id}")
def rename_warehouse(
    wh_id: int, new_name: str = Form(...), db: Session = Depends(database.get_db)
):
    wh = db.query(models.Warehouse).filter(models.Warehouse.id == wh_id).first()
    if wh:
        wh.name = new_name
        db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/warehouse/delete/{wh_id}")
def delete_warehouse(wh_id: int, db: Session = Depends(database.get_db)):
    wh = db.query(models.Warehouse).filter(models.Warehouse.id == wh_id).first()
    if wh:
        wh.is_deleted = True
        db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/receipt")
def receipt(
    warehouse_id: int = Form(...),
    product_id: int = Form(...),
    qty: int = Form(...),
    db: Session = Depends(database.get_db),
):
    if qty <= 0:
        raise HTTPException(400, "Количество должно быть больше 0")
    stock = (
        db.query(models.Stock)
        .filter_by(warehouse_id=warehouse_id, product_id=product_id)
        .first()
    )
    if not stock:
        stock = models.Stock(
            warehouse_id=warehouse_id, product_id=product_id, physical_qty=0
        )
        db.add(stock)
    stock.physical_qty += qty
    db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/transfer/send")
def transfer_send(
    from_wh_id: int = Form(...),
    to_wh_id: int = Form(...),
    product_id: int = Form(...),
    qty: int = Form(...),
    db: Session = Depends(database.get_db),
):
    if qty <= 0:
        raise HTTPException(400, "Количество > 0")
    stock = (
        db.query(models.Stock)
        .filter_by(warehouse_id=from_wh_id, product_id=product_id)
        .first()
    )
    available = (stock.physical_qty - stock.reserved_fbs_qty) if stock else 0

    if available < qty:
        raise HTTPException(
            400,
            f"Недостаточно товара на складе. Доступно: {available} шт. (Резерв FBS блокирует {stock.reserved_fbs_qty if stock else 0} шт.)",
        )

    stock.physical_qty -= qty

    op = models.Operation(
        operation_type=models.OperationType.TRANSFER,
        status=models.OperationStatus.IN_TRANSIT,
        product_id=product_id,
        qty=qty,
        from_wh_id=from_wh_id,
        to_wh_id=to_wh_id,
        idempotency_key=str(uuid.uuid4()),
    )
    db.add(op)
    db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/transfer/receive/{op_id}")
def transfer_receive(op_id: int, db: Session = Depends(database.get_db)):
    op = db.query(models.Operation).filter(models.Operation.id == op_id).first()
    if not op or op.status == models.OperationStatus.COMPLETED:
        return RedirectResponse("/", status_code=303)

    stock = (
        db.query(models.Stock)
        .filter_by(warehouse_id=op.to_wh_id, product_id=op.product_id)
        .first()
    )
    if not stock:
        stock = models.Stock(
            warehouse_id=op.to_wh_id, product_id=op.product_id, physical_qty=0
        )
        db.add(stock)
    stock.physical_qty += op.qty
    op.status = models.OperationStatus.COMPLETED
    db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/fbs/reserve")
def fbs_reserve(
    warehouse_id: int = Form(...),
    product_id: int = Form(...),
    qty: int = Form(...),
    db: Session = Depends(database.get_db),
):
    stock = (
        db.query(models.Stock)
        .filter_by(warehouse_id=warehouse_id, product_id=product_id)
        .first()
    )
    available = (stock.physical_qty - stock.reserved_fbs_qty) if stock else 0
    if available < qty:
        raise HTTPException(
            400, f"Недостаточно доступного товара для FBS. Доступно: {available} шт."
        )

    stock.reserved_fbs_qty += qty
    db.commit()
    return RedirectResponse("/", status_code=303)


# НОВОЕ: Отмена резерва FBS
@app.post("/fbs/cancel")
def fbs_cancel(
    warehouse_id: int = Form(...),
    product_id: int = Form(...),
    qty: int = Form(...),
    db: Session = Depends(database.get_db),
):
    stock = (
        db.query(models.Stock)
        .filter_by(warehouse_id=warehouse_id, product_id=product_id)
        .first()
    )
    if not stock or stock.reserved_fbs_qty < qty:
        raise HTTPException(400, "Недостаточно резерва FBS для отмены")

    stock.reserved_fbs_qty -= qty
    db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/fbs/ship")
def fbs_ship(
    warehouse_id: int = Form(...),
    product_id: int = Form(...),
    qty: int = Form(...),
    db: Session = Depends(database.get_db),
):
    stock = (
        db.query(models.Stock)
        .filter_by(warehouse_id=warehouse_id, product_id=product_id)
        .first()
    )
    if not stock or stock.reserved_fbs_qty < qty:
        raise HTTPException(400, "Недостаточно резерва FBS для отправки")

    stock.physical_qty -= qty
    stock.reserved_fbs_qty -= qty

    op = models.Operation(
        operation_type=models.OperationType.FBS_SHIP,
        status=models.OperationStatus.IN_TRANSIT,
        product_id=product_id,
        qty=qty,
        from_wh_id=warehouse_id,
        idempotency_key=str(uuid.uuid4()),
    )
    db.add(op)
    db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/fbo/ship")
def fbo_ship(
    warehouse_id: int = Form(...),
    product_id: int = Form(...),
    qty: int = Form(...),
    db: Session = Depends(database.get_db),
):
    stock = (
        db.query(models.Stock)
        .filter_by(warehouse_id=warehouse_id, product_id=product_id)
        .first()
    )
    available = (stock.physical_qty - stock.reserved_fbs_qty) if stock else 0
    if available < qty:
        raise HTTPException(
            400, f"Недостаточно товара для FBO. Доступно: {available} шт."
        )

    stock.physical_qty -= qty

    op = models.Operation(
        operation_type=models.OperationType.FBO_SHIP,
        status=models.OperationStatus.IN_TRANSIT,
        product_id=product_id,
        qty=qty,
        from_wh_id=warehouse_id,
        idempotency_key=str(uuid.uuid4()),
    )
    db.add(op)
    db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/fbo/receive/{op_id}")
def fbo_receive(op_id: int, db: Session = Depends(database.get_db)):
    op = db.query(models.Operation).filter(models.Operation.id == op_id).first()
    if op and op.status == models.OperationStatus.IN_TRANSIT:
        op.status = models.OperationStatus.COMPLETED
        db.commit()
    return RedirectResponse("/", status_code=303)
