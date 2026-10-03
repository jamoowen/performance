import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        workers=1,
        access_log=False,
        log_level="warning",
        loop="asyncio",
        http="h11",
    )
