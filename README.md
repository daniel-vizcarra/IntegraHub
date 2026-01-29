# IntegraHub

Plataforma de integración Order-to-Cash (proyecto integrador – Integración de Sistemas).

## Levantar el sistema

```bash
docker compose up -d
```

Un solo comando; no requiere pasos manuales adicionales. La primera vez puede tardar en descargar imágenes y construir.

## Accesos

| Recurso | URL |
|--------|-----|
| **Demo Portal** | http://localhost:8000/static/index.html |
| **API Swagger** | http://localhost:8000/docs |
| **Health** | http://localhost:8000/health |
| **RabbitMQ Management** | http://localhost:15672 (user / password del `.env`) |

## Credenciales demo (Portal / API)

- Usuario: `admin`
- Contraseña: `secret`

Para obtener un JWT: `POST http://localhost:8000/token` con Body `x-www-form-urlencoded`: `username=admin`, `password=secret`.

## Variables de entorno

Copiar `.env.example` a `.env` y completar valores. No subir `.env` al repositorio.

## Servicios

- **postgres**: BD operacional (pedidos, productos)
- **rabbitmq**: Colas `orders`, `orders_pending_restock`, `dead_letter_queue`
- **backend**: API FastAPI (puerto 8000)
- **worker**: Consumidor de pedidos, file watcher CSV, notificaciones

## Documentación y evidencias

En la carpeta `/docs` (o según indicaciones del equipo):

- Diagrama C4 (Context + Container)
- Diagramas de secuencia: Create Order E2E; Fallo + Retry + DLQ
- Matriz de patrones de integración
- Colección Postman (happy path + casos de error)
