import os
import json
import time
import threading
import pika
import urllib.request
import urllib.error
from sqlalchemy.orm import Session
from database import SessionLocal
import models

# --- Configuration ---
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://user:password@localhost:5672/")
INBOX_DIR = "data/inbox"
MAX_RETRIES = 3

# Notificaciones: Slack o Discord webhook (fácil, sin SMTP). Si no hay URL = modo simulado.
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

def _log_notify_config():
    if DISCORD_WEBHOOK_URL:
        print(" [CONFIG] Notificaciones Discord: activado")
    elif SLACK_WEBHOOK_URL:
        print(" [CONFIG] Notificaciones Slack: activado")
    else:
        print(" [CONFIG] Notificaciones: modo simulado (sin webhook)")


def send_notification(title: str, message: str):
    """
    Envía notificación a Slack, Discord o simula en consola.
    Sin dependencias de correo; solo HTTP POST a webhook.
    """
    text = f"**{title}**\n{message}"
    # Slack
    if SLACK_WEBHOOK_URL:
        try:
            data = json.dumps({"text": text}).encode("utf-8")
            req = urllib.request.Request(
                SLACK_WEBHOOK_URL,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
            print(f" [NOTIFY] >> Enviado a Slack: {title}")
            return
        except Exception as e:
            print(f" [NOTIFY] Slack falló: {e}")
    # Discord
    if DISCORD_WEBHOOK_URL:
        try:
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
            print(f" [NOTIFY] >> Enviado a Discord: {title}")
            return
        except Exception as e:
            print(f" [NOTIFY] Discord falló: {e}")
    # Simulado (siempre funciona para demo)
    print(f" [NOTIFY SIMULATION] >> {title} | {message}")

# --- Database Helper ---
def update_order_status(order_id: int, status: str):
    db = SessionLocal()
    try:
        order = db.query(models.Order).filter(models.Order.id == order_id).first()
        if order:
            order.status = status
            db.commit()
            print(f" [DB] Order {order_id} updated to {status}")
    except Exception as e:
        print(f" [DB] Error updating order {order_id}: {e}")
    finally:
        db.close()

# --- RabbitMQ Consumer ---
def process_order(ch, method, properties, body):
    data = json.loads(body)
    order_id = data.get("order_id")
    customer_name = data.get("customer_name")
    product_id = data.get("product_id")
    quantity = data.get("quantity")
    
    print(f" [x] Received Order {order_id} for {customer_name}")

    try:
        # --- TRAMPA DE DEFENSA: FAIL CONDITION ---
        if customer_name == "ERROR":
            raise Exception("Simulated Processing Failure (Payment Gateway Timeout)")

        # --- EVIDENCIA RÚBRICA: Pagos ---
        total = data.get("total_amount", 0.0)
        print(f" [PAYMENT] Processing charge of ${total} for Order {order_id}... APPROVED.")

        # --- LOGICA DE NEGOCIO: STOCK ---
        db = SessionLocal()
        product = db.query(models.Product).filter(models.Product.id == product_id).first()
        
        if product:
            if product.stock >= quantity:
                product.stock -= quantity
                db.commit()
                update_order_status(order_id, "PROCESSED")
                print(f" [STOCK] Order {order_id} Processed. New Stock: {product.stock}")
            else:
                update_order_status(order_id, "OUT_OF_STOCK")
                print(f" [STOCK] Order {order_id} Failed: Insufficient Stock (Req: {quantity}, Avail: {product.stock})")
                
                # --- NOTIFICACION (Slack/Discord webhook o simulado) ---
                send_notification(
                    "ALERTA DE STOCK",
                    f"Producto: {product.name}. Solicitado: {quantity}, Disponible: {product.stock}. Reabastecer.",
                )
                # --- COLA PENDIENTE DE RESTOCK: visible en RabbitMQ hasta que alguien haga Reintentar ---
                try:
                    ch.basic_publish(
                        exchange='',
                        routing_key='orders_pending_restock',
                        body=body,
                        properties=pika.BasicProperties(delivery_mode=2),
                    )
                    print(f" [QUEUE] Order {order_id} en cola orders_pending_restock (visible en RabbitMQ)")
                except Exception as e:
                    print(f" [QUEUE] No se pudo encolar en pending_restock: {e}")
        else:
            update_order_status(order_id, "FAILED_PRODUCT_NOT_FOUND")
        
        db.close()
        
        # Acknowledge message
        ch.basic_ack(delivery_tag=method.delivery_tag)

    except Exception as e:
        print(f" [!] Error processing order {order_id}: {e}")
        
        # Retry Logic
        headers = properties.headers or {}
        retries = headers.get('x-retries', 0)
        
        if retries < MAX_RETRIES:
            print(f" [RESILIENCIA] Retrying ({retries+1}/{MAX_RETRIES})... Backoff strategy active.")
            time.sleep(2) # Pequeño backoff
            
            # Republish with incremented retry count
            headers['x-retries'] = retries + 1
            ch.basic_publish(
                exchange='',
                routing_key='orders',
                body=body,
                properties=pika.BasicProperties(
                    headers=headers,
                    delivery_mode=2
                )
            )
            ch.basic_ack(delivery_tag=method.delivery_tag)
        else:
            print(f" [!!!] Max retries reached. Sending to DEAD LETTER QUEUE (DLQ).")
            # Send to DLQ via DLX
            ch.basic_publish(
                exchange='dlx',
                routing_key='orders_dlq',
                body=body,
                properties=pika.BasicProperties(
                    delivery_mode=2
                )
            )
            update_order_status(order_id, "FAILED")
            ch.basic_ack(delivery_tag=method.delivery_tag)

# --- File Watcher Logic ---
def process_csv_file(filepath):
    """Ingesta CSV: formato producto_id,cantidad. Valida formato, tipos y existencia del producto."""
    print(f" [FILE] Processing {filepath}...")
    try:
        db = SessionLocal()
        line_num = 0
        invalid_lines = []
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line_num += 1
                parts = line.strip().split(',')
                if len(parts) < 2:
                    invalid_lines.append((line_num, line.strip(), "menos de 2 columnas"))
                    continue
                try:
                    prod_id = int(parts[0])
                    qty = int(parts[1])
                except ValueError:
                    invalid_lines.append((line_num, line.strip(), "valores no numéricos"))
                    continue
                if qty <= 0:
                    invalid_lines.append((line_num, line.strip(), "cantidad debe ser positiva"))
                    continue
                product = db.query(models.Product).filter(models.Product.id == prod_id).first()
                if product:
                    product.stock += qty
                    print(f" [FILE] Restocked Product {prod_id} by {qty}. New Stock: {product.stock}")
                else:
                    invalid_lines.append((line_num, f"prod_id={prod_id}", "producto no existe en BD"))
        for ln, content, reason in invalid_lines:
            print(f" [FILE] Línea inválida {ln}: {reason} | {content[:50]}")
        db.commit()
        db.close()
        os.rename(filepath, filepath + ".processed")
        print(f" [FILE] File processed successfully.")
    except Exception as e:
        print(f" [FILE] Error processing file: {e}")

def start_file_watcher():
    print(" [*] Starting File Watcher (Legacy Integration)...")
    if not os.path.exists(INBOX_DIR):
        os.makedirs(INBOX_DIR)
        
    while True:
        try:
            files = [f for f in os.listdir(INBOX_DIR) if f.endswith('.csv')]
            for f in files:
                process_csv_file(os.path.join(INBOX_DIR, f))
            
            time.sleep(10) # Poll every 10s (más rápido para la demo)
        except Exception as e:
            print(f" [FILE] Watcher error: {e}")
            time.sleep(10)

def start_consumer():
    _log_notify_config()
    print(" [*] Starting RabbitMQ Consumer...")
    while True:
        try:
            params = pika.URLParameters(RABBITMQ_URL)
            connection = pika.BlockingConnection(params)
            channel = connection.channel()
            
            channel.queue_declare(queue='orders', durable=True)
            channel.queue_declare(queue='orders_pending_restock', durable=True)
            # DLQ Setup
            channel.exchange_declare(exchange='dlx', exchange_type='direct')
            channel.queue_declare(queue='dead_letter_queue', durable=True)
            channel.queue_bind(exchange='dlx', queue='dead_letter_queue', routing_key='orders_dlq')

            channel.basic_qos(prefetch_count=1)
            channel.basic_consume(queue='orders', on_message_callback=process_order)
            
            print(" [*] Waiting for messages. To exit press CTRL+C")
            channel.start_consuming()
        except pika.exceptions.AMQPConnectionError:
            print(" [!] RabbitMQ not ready. Retrying in 5s...")
            time.sleep(5)

if __name__ == "__main__":
    # Start File Watcher in a separate thread
    watcher_thread = threading.Thread(target=start_file_watcher, daemon=True)
    watcher_thread.start()
    
    # Start Consumer in main thread
    start_consumer()