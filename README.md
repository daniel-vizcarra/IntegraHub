# IntegraHub

Plataforma de integración Order-to-Cash para el flujo de pedidos: APIs, mensajería asíncrona (RabbitMQ), integración por archivos CSV y analítica. Proyecto integrador de la asignatura Integración de Sistemas.

---

## Requisitos previos

Para replicar el proyecto en su máquina debe tener instalado:

| Requisito | Descripción | Comprobación |
|-----------|-------------|--------------|
| **Docker** | Motor de contenedores (versión 20.10 o superior recomendada). | `docker --version` |
| **Docker Compose** | Orquestación de servicios (v2 o superior; puede venir integrado con Docker Desktop). | `docker compose version` |
| **Git** | Para clonar el repositorio. | `git --version` |

En Windows puede usar [Docker Desktop](https://www.docker.com/products/docker-desktop/), que incluye Docker y Docker Compose. En Linux, instale Docker y el plugin Compose según la documentación oficial.

No es necesario tener Python ni PostgreSQL instalados en el host: la aplicación y sus dependencias se ejecutan dentro de contenedores.

---

## Obtención del código

Clone el repositorio en su equipo:

```bash
git clone <URL_DEL_REPOSITORIO> IntegraHub
cd IntegraHub
```

Sustituya `<URL_DEL_REPOSITORIO>` por la URL real del repositorio (por ejemplo en GitHub o GitLab).

---

## Configuración

1. Cree el archivo de variables de entorno a partir del ejemplo:

   ```bash
   cp .env.example .env
   ```

2. Edite `.env` y asigne los valores que desee. Las variables mínimas son:

   | Variable | Descripción | Valor por defecto (en código) |
   |----------|-------------|--------------------------------|
   | `POSTGRES_USER` | Usuario de PostgreSQL | `user` |
   | `POSTGRES_PASSWORD` | Contraseña de PostgreSQL | Definir en `.env` |
   | `POSTGRES_DB` | Nombre de la base de datos | `integrahub` |
   | `RABBITMQ_USER` | Usuario de RabbitMQ | `user` |
   | `RABBITMQ_PASSWORD` | Contraseña de RabbitMQ | Definir en `.env` |

   Las variables opcionales `DISCORD_WEBHOOK_URL`, `SLACK_WEBHOOK_URL` y `SECRET_KEY` se describen en `.env.example`. Si no se definen, las notificaciones se simulan en consola y el JWT usa una clave de demo.

3. No suba el archivo `.env` al repositorio (contiene datos sensibles). El archivo `.gitignore` ya lo excluye.

---

## Ejecución

1. Desde la raíz del proyecto (donde está `docker-compose.yml`), ejecute:

   ```bash
   docker compose up -d
   ```

2. La primera vez se descargarán las imágenes y se construirá la aplicación. Espere a que todos los servicios estén en ejecución (unos segundos o minutos según su conexión).

3. Para comprobar el estado de los contenedores:

   ```bash
   docker compose ps
   ```

   Los cuatro servicios (postgres, rabbitmq, backend, worker) deben aparecer como "running".

---

## Detención del sistema

Para detener todos los servicios:

```bash
docker compose down
```

Para detener y eliminar además los volúmenes (incluidos los datos de PostgreSQL):

```bash
docker compose down -v
```

---

## Servicios y puertos

| Servicio | Puerto(s) en el host | Descripción |
|----------|----------------------|-------------|
| **postgres** | 5432 | Base de datos PostgreSQL (pedidos, productos). |
| **rabbitmq** | 5672 (AMQP), 15672 (Management UI) | Broker de mensajes y consola web. |
| **backend** | 8000 | API REST (FastAPI) y archivos estáticos del Demo Portal. |
| **worker** | — | Procesador de la cola de pedidos y file watcher para CSV. |

Asegúrese de que los puertos 5432, 5672, 15672 y 8000 no estén en uso por otras aplicaciones antes de ejecutar `docker compose up -d`.

---

## Accesos tras levantar el sistema

| Recurso | URL | Notas |
|---------|-----|--------|
| **Demo Portal** | http://localhost:8000/static/index.html | Interfaz web para pedidos, productos y analítica. |
| **Documentación API (Swagger)** | http://localhost:8000/docs | Prueba de endpoints y esquemas. |
| **Estado del sistema (Health)** | http://localhost:8000/health | Estado de PostgreSQL y RabbitMQ. |
| **RabbitMQ Management** | http://localhost:15672 | Usuario y contraseña según `RABBITMQ_USER` y `RABBITMQ_PASSWORD` en `.env`. |

---

## Credenciales de demostración

Para el Demo Portal y para obtener un token JWT contra la API:

- **Usuario:** `admin`
- **Contraseña:** `secret`

Para usar la API desde Swagger o Postman: en **POST /token** envíe en el cuerpo (formulario `x-www-form-urlencoded`) los campos `username=admin` y `password=secret`. La respuesta incluye `access_token` (JWT). Use el valor en el header `Authorization: Bearer <access_token>` en las peticiones protegidas.

---

## Integración por archivos (CSV)

El worker monitorea la carpeta `data/inbox` en busca de archivos con extensión `.csv`. Formato esperado por línea:

```text
producto_id,cantidad
```

Ejemplo:

```text
1,10
2,5
```

- `producto_id` debe coincidir con un producto existente en la base de datos.
- `cantidad` debe ser un entero positivo (se suma al stock actual).
- Las líneas que no cumplan formato o reglas se registran en los logs del worker; el archivo se procesa y luego se renombra a `.csv.processed`.

Para probar: cree un archivo `data/inbox/restock.csv` con el contenido anterior y espere el siguiente ciclo del watcher (cada 10 segundos) o reinicie el contenedor `worker`.

---

## Notificaciones (opcional)

Si en `.env` se define `DISCORD_WEBHOOK_URL` o `SLACK_WEBHOOK_URL`, las alertas de stock insuficiente se envían a ese canal. Si no se define ninguna, las notificaciones se simulan en la salida estándar del contenedor `worker`. Para ver los logs del worker:

```bash
docker compose logs -f worker
```

---

## Estructura del proyecto

```text
IntegraHub/
├── docker-compose.yml    # Definición de servicios (postgres, rabbitmq, backend, worker)
├── Dockerfile           # Imagen para backend y worker (Python 3.9)
├── requirements.txt     # Dependencias Python
├── .env.example         # Plantilla de variables de entorno (copiar a .env)
├── main.py              # API FastAPI: pedidos, productos, JWT, health, analítica
├── worker.py            # Consumidor RabbitMQ, file watcher CSV, notificaciones
├── database.py           # Conexión y sesión SQLAlchemy (PostgreSQL)
├── models.py            # Modelos ORM (Order, Product)
├── schemas.py           # Esquemas Pydantic (entrada/salida API)
├── static/
│   └── index.html       # Demo Portal (interfaz web)
├── data/
│   └── inbox/           # Carpeta de entrada para archivos CSV
└── docs/                # Documentación, informe, matriz de patrones
```

---

## Solución de problemas

- **Los contenedores no arrancan:** Verifique que Docker esté en ejecución y que los puertos 5432, 5672, 15672 y 8000 estén libres. Ejecute `docker compose logs` para ver errores por servicio.
- **Error de conexión a la base de datos:** Asegúrese de que los valores de `POSTGRES_USER`, `POSTGRES_PASSWORD` y `POSTGRES_DB` en `.env` coincidan con los que usa el servicio `postgres` en `docker-compose.yml`. Tras cambiar `.env`, ejecute `docker compose down` y `docker compose up -d`.
- **El Demo Portal no carga o devuelve 401:** Debe iniciar sesión con las credenciales indicadas (admin / secret). Si modificó `SECRET_KEY` después de haber obtenido un token, invalide la sesión (por ejemplo borrando el token en el almacenamiento local del navegador) y vuelva a iniciar sesión.
- **RabbitMQ no acepta conexiones:** Espere a que el servicio pase su healthcheck (varios segundos tras `docker compose up -d`). El backend y el worker dependen de que RabbitMQ esté listo.

---
