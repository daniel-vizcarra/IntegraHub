import os
import json
import pika
from datetime import datetime, timedelta
from jose import JWTError, jwt
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List, Optional

import models
import schemas
from database import engine, get_db

# JWT (requisito consigna: OAuth2 + JWT)
SECRET_KEY = os.getenv("SECRET_KEY", "integrahub-demo-secret-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 horas para demo

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

# Create tables
models.Base.metadata.create_all(bind=engine)

# Initialize Stock
def init_db():
    db = next(get_db())
    # Create default product if not exists
    existing_product = db.query(models.Product).filter(models.Product.id == 1).first()
    if not existing_product:
        product = models.Product(id=1, name="Laptop Gamer", price=1500.0, stock=10)
        db.add(product)
        db.commit()
        print(" [INIT] Created default product: Laptop Gamer")
    else:
        print(" [INIT] Default product already exists.")

init_db()

app = FastAPI(title="IntegraHub API")

# Mount Static Files
app.mount("/static", StaticFiles(directory="static"), name="static")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# RabbitMQ Connection
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://user:password@localhost:5672/")
connection = None
channel = None

def _close_rabbitmq():
    global connection, channel
    try:
        if channel:
            channel.close()
    except Exception:
        pass
    try:
        if connection:
            connection.close()
    except Exception:
        pass
    connection = None
    channel = None

QUEUE_PENDING_RESTOCK = "orders_pending_restock"

def get_rabbitmq_channel():
    global connection, channel
    if connection is None or connection.is_closed:
        _close_rabbitmq()
        params = pika.URLParameters(RABBITMQ_URL)
        connection = pika.BlockingConnection(params)
        channel = connection.channel()
        channel.queue_declare(queue='orders', durable=True)
        channel.queue_declare(queue=QUEUE_PENDING_RESTOCK, durable=True)
        channel.exchange_declare(exchange='dlx', exchange_type='direct')
        channel.queue_declare(queue='dead_letter_queue', durable=True)
        channel.queue_bind(exchange='dlx', queue='dead_letter_queue', routing_key='orders_dlq')
    return channel

def remove_order_from_pending_restock(order_id: int) -> bool:
    """Quita un mensaje con este order_id de orders_pending_restock (para no duplicar al republicar)."""
    try:
        ch = get_rabbitmq_channel()
        for _ in range(1000):
            method, _, body = ch.basic_get(queue=QUEUE_PENDING_RESTOCK, auto_ack=False)
            if method is None:
                return False
            data = json.loads(body)
            if data.get("order_id") == order_id:
                ch.basic_ack(delivery_tag=method.delivery_tag)
                return True
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
        return False
    except Exception as e:
        print(f" [!] remove_order_from_pending_restock: {e}")
        return False

def publish_order_to_queue(order_id: int, customer_name: str, cedula: str, product_id: int, quantity: int, total_amount: float) -> bool:
    """Publica un pedido en la cola 'orders'. Retorna True si OK, False si falló."""
    message = json.dumps({
        "order_id": order_id,
        "customer_name": customer_name,
        "cedula": cedula,
        "product_id": product_id,
        "quantity": quantity,
        "total_amount": total_amount,
    })
    for attempt in range(2):
        try:
            ch = get_rabbitmq_channel()
            ch.basic_publish(
                exchange='',
                routing_key='orders',
                body=message,
                properties=pika.BasicProperties(delivery_mode=2),
            )
            print(f" [x] Sent order {order_id} to queue")
            return True
        except Exception as e:
            print(f" [!] Publish attempt {attempt + 1} failed: {e}")
            _close_rabbitmq()
    return False

# OAuth2 + JWT (requisito consigna: al menos 1 API protegida con OAuth2 + JWT)
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

def get_current_user(token: str = Depends(oauth2_scheme)):
    """Verifica el JWT: firma, expiración y sub. Token inválido o expirado → 401."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return username
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

@app.post("/token", response_model=schemas.Token)
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    """Login OAuth2: cualquier usuario/contraseña para demo. Devuelve un JWT firmado con expiración."""
    access_token = create_access_token(data={"sub": form_data.username})
    return {"access_token": access_token, "token_type": "bearer"}

@app.post("/orders", response_model=schemas.OrderResponse, status_code=status.HTTP_201_CREATED)
def create_order(
    order: schemas.OrderCreate, 
    db: Session = Depends(get_db), 
    token: str = Depends(get_current_user)
):
    # 1. Calculate Total
    product = db.query(models.Product).filter(models.Product.id == order.product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    total = product.price * order.quantity

    # 2. Save to DB (Status: CREATED)
    db_order = models.Order(**order.dict(), total_amount=total, status="CREATED")
    db.add(db_order)
    db.commit()
    db.refresh(db_order)

    # 3. Publish to RabbitMQ (reintento + reconexión si falla)
    if not publish_order_to_queue(
        db_order.id, db_order.customer_name, db_order.cedula,
        db_order.product_id, db_order.quantity, total
    ):
        db_order.status = "FAILED_QUEUE"
        db.add(db_order)
        db.commit()
        db.refresh(db_order)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No se pudo encolar el pedido. Intente de nuevo o use 'Reintentar' más tarde.",
        )
    return db_order

@app.get("/orders", response_model=List[schemas.OrderResponse])
def read_orders(skip: int = 0, limit: int = 100, db: Session = Depends(get_db), token: str = Depends(get_current_user)):
    orders = db.query(models.Order).order_by(models.Order.id.desc()).offset(skip).limit(limit).all()
    return orders

@app.post("/orders/republish-created")
def republish_created_orders(db: Session = Depends(get_db), token: str = Depends(get_current_user)):
    """Republica a la cola todos los pedidos en CREATED o FAILED_QUEUE (para recuperar los que no se encolaron)."""
    orders = db.query(models.Order).filter(
        models.Order.status.in_(["CREATED", "FAILED_QUEUE"])
    ).all()
    republished = 0
    for o in orders:
        if publish_order_to_queue(o.id, o.customer_name, o.cedula, o.product_id, o.quantity, o.total_amount):
            o.status = "CREATED"
            db.add(o)
            republished += 1
    db.commit()
    return {"republished": republished, "total": len(orders)}

@app.post("/orders/{order_id}/republish")
def republish_order(order_id: int, db: Session = Depends(get_db), token: str = Depends(get_current_user)):
    """Republica un pedido en CREATED, FAILED_QUEUE u OUT_OF_STOCK para que el worker lo procese."""
    order = db.query(models.Order).filter(models.Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status not in ("CREATED", "FAILED_QUEUE", "OUT_OF_STOCK"):
        raise HTTPException(
            status_code=400,
            detail=f"No se puede republicar: estado actual es {order.status}",
        )
    # Si era OUT_OF_STOCK, quitamos su mensaje de la cola pending_restock (para no duplicar)
    if order.status == "OUT_OF_STOCK":
        remove_order_from_pending_restock(order_id)
    if not publish_order_to_queue(order.id, order.customer_name, order.cedula, order.product_id, order.quantity, order.total_amount):
        raise HTTPException(status_code=503, detail="No se pudo encolar. Intente más tarde.")
    order.status = "CREATED"
    db.add(order)
    db.commit()
    db.refresh(order)
    return {"ok": True, "message": "Pedido encolado. El worker lo procesará en breve.", "order_id": order_id}

@app.get("/products", response_model=List[schemas.ProductResponse])
def read_products(db: Session = Depends(get_db), token: str = Depends(get_current_user)):
    return db.query(models.Product).all()

@app.post("/products", response_model=schemas.ProductResponse, status_code=status.HTTP_201_CREATED)
def create_product(product: schemas.ProductCreate, db: Session = Depends(get_db), token: str = Depends(get_current_user)):
    db_product = models.Product(**product.dict())
    db.add(db_product)
    db.commit()
    db.refresh(db_product)
    return db_product

# --- Health / System Status (requisito consigna: mecanismo "sistema vivo") ---
@app.get("/health")
def health():
    """Estado por servicio: Postgres y RabbitMQ. Para Demo Portal / defensa."""
    from sqlalchemy import text
    services = {}
    try:
        db = next(get_db())
        db.execute(text("SELECT 1"))
        db.close()
        services["postgres"] = "ok"
    except Exception as e:
        services["postgres"] = f"error: {str(e)[:80]}"
    try:
        ch = get_rabbitmq_channel()
        ch.queue_declare(queue="orders", passive=True)
        services["rabbitmq"] = "ok"
    except Exception as e:
        services["rabbitmq"] = f"error: {str(e)[:80]}"
    status_overall = "ok" if all(v == "ok" for v in services.values()) else "degraded"
    return {"status": status_overall, "services": services}

# --- Analítica (Flujo D consigna: extracción desde BD para métricas) ---
@app.get("/analytics")
def analytics(db: Session = Depends(get_db), token: str = Depends(get_current_user)):
    """
    Métricas agregadas desde la BD operacional (batch/analítica).
    Evidencia Flujo D: extracción y consolidación para reportes.
    """
    # Pedidos por estado
    by_status = db.query(models.Order.status, func.count(models.Order.id)).group_by(models.Order.status).all()
    orders_by_status = {row[0]: row[1] for row in by_status}
    # Ingresos (solo pedidos procesados)
    total_revenue = db.query(func.coalesce(func.sum(models.Order.total_amount), 0)).filter(
        models.Order.status == "PROCESSED"
    ).scalar()
    total_revenue = float(total_revenue) if total_revenue is not None else 0.0
    # Total pedidos
    total_orders = db.query(func.count(models.Order.id)).scalar() or 0
    # Últimos 7 días
    since = datetime.utcnow() - timedelta(days=7)
    orders_last_7_days = db.query(func.count(models.Order.id)).filter(models.Order.created_at >= since).scalar() or 0
    return {
        "total_orders": total_orders,
        "total_revenue": round(total_revenue, 2),
        "orders_by_status": orders_by_status,
        "orders_last_7_days": orders_last_7_days,
    }

# --- PDF INVOICE GENERATION ---
from fpdf import FPDF
from fastapi.responses import StreamingResponse
import io

@app.get("/orders/{order_id}/invoice")
def generate_invoice(order_id: int, db: Session = Depends(get_db)):
    order = db.query(models.Order).filter(models.Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    product = db.query(models.Product).filter(models.Product.id == order.product_id).first()
    product_name = product.name if product else "Unknown Product"

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", size=12)
    
    # Header
    pdf.set_font("Arial", 'B', 16)
    pdf.cell(200, 10, txt="IntegraHub ERP - Factura Electronica", ln=True, align='C')
    pdf.ln(10)
    
    # Details
    pdf.set_font("Arial", size=12)
    pdf.cell(200, 10, txt=f"Orden ID: #{order.id}", ln=True)
    pdf.cell(200, 10, txt=f"Fecha: {order.created_at}", ln=True)
    pdf.cell(200, 10, txt=f"Cliente: {order.customer_name}", ln=True)
    pdf.cell(200, 10, txt=f"Cedula/NIT: {order.cedula}", ln=True)
    pdf.ln(10)
    
    # Table Header
    pdf.set_fill_color(200, 220, 255)
    pdf.cell(100, 10, "Producto", 1, 0, 'C', 1)
    pdf.cell(30, 10, "Cant", 1, 0, 'C', 1)
    pdf.cell(60, 10, "Total", 1, 1, 'C', 1)
    
    # Table Body
    pdf.cell(100, 10, product_name, 1)
    pdf.cell(30, 10, str(order.quantity), 1)
    pdf.cell(60, 10, f"${order.total_amount}", 1, 1)
    
    # Footer
    pdf.ln(20)
    pdf.set_font("Arial", 'I', 10)
    pdf.cell(200, 10, txt="Gracias por su compra. Este documento es un comprobante valido.", ln=True, align='C')

    # Output
    # FPDF 1.7.2 output(dest='S') returns a string (latin-1 encoded)
    pdf_string = pdf.output(dest='S')
    buffer = io.BytesIO(pdf_string.encode('latin-1'))
    buffer.seek(0)
    
    return StreamingResponse(buffer, media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename=invoice_{order_id}.pdf"})

# --- Prueba de notificación Discord (para verificar webhook) ---
import urllib.request

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

@app.get("/notify-test")
def notify_test():
    """Envía un mensaje de prueba a Discord. Sirve para verificar que el webhook funciona."""
    if not DISCORD_WEBHOOK_URL:
        raise HTTPException(status_code=503, detail="DISCORD_WEBHOOK_URL no configurada en el servidor.")
    try:
        text = "**IntegraHub – Mensaje de prueba**\nSi ves esto, el webhook de Discord está funcionando."
        data = json.dumps({"content": text}).encode("utf-8")
        req = urllib.request.Request(
            DISCORD_WEBHOOK_URL,
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "IntegraHub/1.0 (Discord-Webhook)",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
        return {"ok": True, "message": "Mensaje de prueba enviado a Discord. Revisa el canal."}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Discord webhook falló: {str(e)}")
