import uvicorn

from askai.app import app
from askai.config import load_config

if __name__ == "__main__":
    config = load_config()
    uvicorn.run(app, host=config.host, port=config.port)
