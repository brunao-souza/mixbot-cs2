import os
from aiohttp import web

async def start_healthcheck():
    app = web.Application()
    app.router.add_get("/health", lambda _: web.Response(text="ok"))

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    print(f"🌐 Healthcheck ativo em /health na porta {port}") 
