"""
ACL Manager External App для Nextcloud 30+
Использует AppAPI для интеграции с Nextcloud
"""

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import httpx
import os
from typing import Optional
import logging

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("acl_manager")

# Конфигурация
NEXTCLOUD_URL = os.getenv("NEXTCLOUD_URL", "")
APP_SECRET = os.getenv("APP_SECRET", "")
APP_ID = "acl_manager"
APP_VERSION = "2.0.0"
APP_PORT = int(os.getenv("APP_PORT", "9030"))

# Windows Agent настройки
AGENT_URL = os.getenv("AGENT_URL", "")
AGENT_BEARER_TOKEN = os.getenv("AGENT_BEARER_TOKEN", "")
AGENT_CERT_PATH = os.getenv("AGENT_CERT_PATH", "")
AGENT_CERT_PASSWORD = os.getenv("AGENT_CERT_PASSWORD", "")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle: регистрация/дерегистрация в Nextcloud"""
    logger.info(f"Starting {APP_ID} v{APP_VERSION}")
    
    # При старте регистрируем ExApp в Nextcloud
    if NEXTCLOUD_URL and APP_SECRET:
        await register_exapp()
    
    yield
    
    logger.info(f"Shutting down {APP_ID}")

app = FastAPI(
    title="ACL Manager ExApp",
    version=APP_VERSION,
    lifespan=lifespan
)

# CORS для Nextcloud
app.add_middleware(
    CORSMiddleware,
    allow_origins=[NEXTCLOUD_URL] if NEXTCLOUD_URL else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ══════════════════════════════════════════════════════════════════════
# Утилиты для работы с Nextcloud
# ══════════════════════════════════════════════════════════════════════

async def verify_nextcloud_request(request: Request) -> dict:
    """
    Проверяет что запрос пришёл от Nextcloud и извлекает контекст пользователя
    """
    auth_header = request.headers.get("Authorization", "")
    
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid authorization")
    
    token = auth_header[7:]  # Убираем "Bearer "
    
    if token != APP_SECRET:
        raise HTTPException(status_code=401, detail="Invalid token")
    
    # Извлекаем контекст пользователя из заголовков
    user_id = request.headers.get("X-Nextcloud-User-Id", "")
    user_display_name = request.headers.get("X-Nextcloud-User-Display-Name", "")
    user_groups = request.headers.get("X-Nextcloud-User-Groups", "").split(",")
    
    if not user_id:
        raise HTTPException(status_code=401, detail="User context missing")
    
    return {
        "user_id": user_id,
        "display_name": user_display_name,
        "groups": [g.strip() for g in user_groups if g.strip()],
    }

async def call_agent(
    method: str,
    endpoint: str,
    user_context: dict,
    body: Optional[dict] = None,
    params: Optional[dict] = None
) -> dict:
    """
    Выполняет запрос к Windows Agent с mTLS и Bearer аутентификацией
    """
    url = f"{AGENT_URL.rstrip('/')}{endpoint}"
    
    headers = {
        "Authorization": f"Bearer {AGENT_BEARER_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Nc-User": user_context["user_id"],
        "X-Nc-User-Groups": ",".join(user_context["groups"]),
    }
    
    # Настройка mTLS сертификата
    cert = None
    if AGENT_CERT_PATH:
        if AGENT_CERT_PASSWORD:
            cert = (AGENT_CERT_PATH, AGENT_CERT_PASSWORD)
        else:
            cert = AGENT_CERT_PATH
    
    async with httpx.AsyncClient(cert=cert, verify=True) as client:
        try:
            if method == "GET":
                response = await client.get(url, headers=headers, params=params, timeout=30.0)
            elif method == "POST":
                response = await client.post(url, headers=headers, json=body, timeout=30.0)
            elif method == "DELETE":
                response = await client.delete(url, headers=headers, json=body, timeout=30.0)
            else:
                raise ValueError(f"Unsupported method: {method}")
            
            response.raise_for_status()
            return response.json()
            
        except httpx.HTTPStatusError as e:
            logger.error(f"Agent HTTP error: {e.response.status_code} - {e.response.text}")
            raise HTTPException(
                status_code=502,
                detail=f"Agent error: {e.response.status_code}"
            )
        except httpx.RequestError as e:
            logger.error(f"Agent connection error: {str(e)}")
            raise HTTPException(
                status_code=502,
                detail=f"Cannot connect to agent: {str(e)}"
            )

# ══════════════════════════════════════════════════════════════════════
# ExApp регистрация
# ══════════════════════════════════════════════════════════════════════

async def register_exapp():
    """
    Регистрирует ExApp в Nextcloud 34 через AppAPI
    В NC 34 используется новый формат регистрации
    """
    if not NEXTCLOUD_URL or not APP_SECRET:
        logger.warning("NEXTCLOUD_URL or APP_SECRET not set, skipping registration")
        return
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # В NC 34 регистрация происходит через init endpoint
            response = await client.post(
                f"{NEXTCLOUD_URL}/ocs/v1.php/apps/app_api/api/v1/ex-app/init",
                json={
                    "app_id": APP_ID,
                    "app_version": APP_VERSION,
                    "app_name": "ACL Manager",
                    "app_enabled": True,
                },
                headers={
                    "EX-APP-ID": APP_ID,
                    "EX-APP-VERSION": APP_VERSION,
                    "AUTHORIZATION-APP-API": APP_SECRET,
                    "OCS-APIRequest": "true",
                    "Accept": "application/json",
                },
            )
            
            if response.status_code in [200, 201]:
                logger.info("Successfully initialized with Nextcloud 34")
                # Регистрируем UI компоненты
                await register_ui_components(client)
            else:
                logger.warning(f"Init response: {response.status_code} - {response.text}")
                
    except Exception as e:
        logger.error(f"Failed to register with Nextcloud: {str(e)}")

async def register_ui_components(client: httpx.AsyncClient):
    """
    Регистрирует UI компоненты в Nextcloud 34:
    - Files action (контекстное меню)
    - Files sidebar tab
    """
    try:
        # Регистрация Files Action для папок
        await client.post(
            f"{NEXTCLOUD_URL}/ocs/v1.php/apps/app_api/api/v1/ui/files-action",
            json={
                "actionName": "acl_manager_action",
                "actionDisplayName": "ACL / Права доступа",
                "actionHandler": "/ui/files-action",
                "icon": "icon-lock",
                "mime": "httpd/unix-directory",  # Только для папок
                "permissions": 31,  # Все права
            },
            headers={
                "EX-APP-ID": APP_ID,
                "EX-APP-VERSION": APP_VERSION,
                "AUTHORIZATION-APP-API": APP_SECRET,
                "OCS-APIRequest": "true",
            },
        )
        logger.info("Registered files action")
        
        # Регистрация Sidebar Tab
        await client.post(
            f"{NEXTCLOUD_URL}/ocs/v1.php/apps/app_api/api/v1/ui/files-sidebar",
            json={
                "tabName": "acl_manager_tab",
                "tabDisplayName": "ACL",
                "tabHandler": "/ui/sidebar",
                "icon": "icon-lock",
                "mime": "httpd/unix-directory",
            },
            headers={
                "EX-APP-ID": APP_ID,
                "EX-APP-VERSION": APP_VERSION,
                "AUTHORIZATION-APP-API": APP_SECRET,
                "OCS-APIRequest": "true",
            },
        )
        logger.info("Registered sidebar tab")
        
    except Exception as e:
        logger.error(f"Failed to register UI components: {str(e)}")

# ══════════════════════════════════════════════════════════════════════
# API Endpoints
# ══════════════════════════════════════════════════════════════════════

@app.get("/")
async def root():
    """Health check endpoint"""
    return {
        "app": APP_ID,
        "version": APP_VERSION,
        "status": "running"
    }

@app.get("/enabled")
async def enabled(user: dict = Depends(verify_nextcloud_request)):
    """
    Nextcloud вызывает этот endpoint чтобы проверить, должен ли ExApp 
    быть доступен для конкретного пользователя
    """
    # Здесь можно добавить логику проверки прав
    # Например, только для админов или определённых групп
    return {"enabled": True}

# ── ACL API ───────────────────────────────────────────────────────────

@app.get("/api/acl")
async def get_acl(
    path: str,
    user: dict = Depends(verify_nextcloud_request)
):
    """Получить ACL для папки"""
    result = await call_agent("GET", "/api/acl", user, params={"path": path})
    return result

@app.post("/api/acl")
async def set_acl(
    request: Request,
    user: dict = Depends(verify_nextcloud_request)
):
    """Установить ACL правило"""
    body = await request.json()
    body["initiatedByUser"] = user["user_id"]
    result = await call_agent("POST", "/api/acl", user, body=body)
    return result

@app.delete("/api/acl")
async def remove_acl(
    request: Request,
    user: dict = Depends(verify_nextcloud_request)
):
    """Удалить ACL правило"""
    body = await request.json()
    body["initiatedByUser"] = user["user_id"]
    result = await call_agent("DELETE", "/api/acl", user, body=body)
    return result

# ── Groups API ────────────────────────────────────────────────────────

@app.get("/api/groups")
async def get_folder_groups(
    path: str,
    user: dict = Depends(verify_nextcloud_request)
):
    """Получить группы доступа для папки"""
    result = await call_agent("GET", "/api/groups", user, params={"path": path})
    return result

@app.post("/api/groups")
async def create_folder_groups(
    request: Request,
    user: dict = Depends(verify_nextcloud_request)
):
    """Создать группы RO/RX/RW для папки"""
    body = await request.json()
    body["initiatedByUser"] = user["user_id"]
    result = await call_agent("POST", "/api/groups", user, body=body)
    return result

@app.delete("/api/groups")
async def delete_folder_groups(
    path: str,
    user: dict = Depends(verify_nextcloud_request)
):
    """Удалить все группы для папки"""
    body = {
        "folderPath": path,
        "initiatedByUser": user["user_id"]
    }
    result = await call_agent("DELETE", "/api/groups", user, body=body)
    return result

@app.get("/api/groups/{group_name}/members")
async def get_group_members(
    group_name: str,
    user: dict = Depends(verify_nextcloud_request)
):
    """Получить членов группы"""
    result = await call_agent("GET", f"/api/groups/{group_name}/members", user)
    return result

@app.post("/api/groups/{group_name}/members")
async def add_group_member(
    group_name: str,
    request: Request,
    user: dict = Depends(verify_nextcloud_request)
):
    """Добавить пользователя в группу"""
    body = await request.json()
    result = await call_agent("POST", f"/api/groups/{group_name}/members", user, body=body)
    return result

@app.delete("/api/groups/{group_name}/members/{user_sam}")
async def remove_group_member(
    group_name: str,
    user_sam: str,
    user: dict = Depends(verify_nextcloud_request)
):
    """Удалить пользователя из группы"""
    body = {"comment": None}
    result = await call_agent("DELETE", f"/api/groups/{group_name}/members/{user_sam}", user, body=body)
    return result

# ── Users API ─────────────────────────────────────────────────────────

@app.get("/api/users/search")
async def search_users(
    q: str,
    max: int = 20,
    user: dict = Depends(verify_nextcloud_request)
):
    """Поиск пользователей в AD"""
    result = await call_agent("GET", "/api/users/search", user, params={"q": q, "max": max})
    return result

@app.get("/api/users/{sam}/manager-chain")
async def get_manager_chain(
    sam: str,
    user: dict = Depends(verify_nextcloud_request)
):
    """Получить цепочку руководителей"""
    result = await call_agent("GET", f"/api/users/{sam}/manager-chain", user)
    return result

# ── Settings / Mounts ─────────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings(user: dict = Depends(verify_nextcloud_request)):
    """Получить настройки пользователя (права доступа)"""
    # TODO: интеграция с Nextcloud для проверки admin прав
    return {
        "is_admin": True,  # Временно все админы
        "owner_mode_enabled": False
    }

@app.get("/api/mounts")
async def get_mounts(user: dict = Depends(verify_nextcloud_request)):
    """Получить маппинги NC путей → UNC путей"""
    # TODO: загрузка из конфига или Nextcloud
    return {
        "mounts": [
            {
                "ncPath": "/Documents",
                "uncPath": "\\\\SERVER\\Documents"
            },
            {
                "ncPath": "/Shared",
                "uncPath": "\\\\SERVER\\Shared"
            }
        ]
    }

# ══════════════════════════════════════════════════════════════════════
# UI Endpoints для Nextcloud 34 интеграции
# ══════════════════════════════════════════════════════════════════════

@app.post("/ui/files-action")
async def files_action_handler(
    request: Request,
    user: dict = Depends(verify_nextcloud_request)
):
    """
    Обработчик клика на Files Action.
    Nextcloud отправляет данные о выбранной папке.
    Возвращаем команду открыть sidebar.
    """
    body = await request.json()
    file_info = body.get("fileInfo", {})
    
    logger.info(f"Files action clicked: {file_info.get('path', 'unknown')}")
    
    # Возвращаем команду Nextcloud открыть sidebar
    return {
        "action": "open_sidebar",
        "tab": "acl_manager_tab"
    }

@app.get("/ui/sidebar")
async def sidebar_handler(
    path: str,
    user: dict = Depends(verify_nextcloud_request)
):
    """
    Отдаёт HTML для sidebar.
    Nextcloud загружает это содержимое в iframe.
    """
    from fastapi.responses import HTMLResponse
    
    # Получаем UNC путь (нужно будет добавить логику маппинга)
    unc_path = path  # TODO: маппинг NC path -> UNC path
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>ACL Manager</title>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                margin: 0;
                padding: 16px;
                background: var(--color-main-background, #fff);
                color: var(--color-main-text, #000);
            }}
            .header {{
                display: flex;
                align-items: center;
                gap: 8px;
                margin-bottom: 16px;
                font-weight: 600;
            }}
            .path {{
                padding: 8px;
                background: var(--color-background-hover, #f5f5f5);
                border-radius: 4px;
                font-size: 12px;
                font-family: monospace;
                margin-bottom: 16px;
                word-break: break-all;
            }}
            .loading {{
                text-align: center;
                padding: 32px;
                color: var(--color-text-maxcontrast, #999);
            }}
            #app {{
                min-height: 400px;
            }}
        </style>
    </head>
    <body>
        <div id="app">
            <div class="header">
                <span>🔒</span>
                <span>ACL / Права доступа</span>
            </div>
            <div class="path">{unc_path}</div>
            <div class="loading">Загрузка...</div>
        </div>
        
        <script>
            // Здесь будет Vue.js приложение
            const APP_API_URL = window.location.origin;
            const FOLDER_PATH = '{unc_path}';
            
            // TODO: загрузить Vue компоненты и отобразить панель ACL
            console.log('ACL Manager loaded for:', FOLDER_PATH);
            
            // Временно - просто показываем что загрузилось
            setTimeout(() => {{
                document.querySelector('.loading').textContent = 
                    'Панель ACL будет здесь. Путь: ' + FOLDER_PATH;
            }}, 500);
        </script>
    </body>
    </html>
    """
    
    return HTMLResponse(content=html)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=APP_PORT)
