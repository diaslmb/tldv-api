import os
import httpx
from loguru import logger

# === CONFIG FOR YOUR WORKFLOW AGENT ===
API_KEY = "app-GMysC0py6j6HQJsJSxI2Rbxb"
BASE_URL = "https://shai.pro/v1"

class WorkflowAgentProcessor:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url
        self.api_key = api_key
        self.headers = {"Authorization": f"Bearer {self.api_key}"}

    async def upload_file(self, filepath: str) -> str:
        """Uploads one .txt file and returns its file_id"""
        url = f"{self.base_url}/files/upload"
        filename = os.path.basename(filepath)
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                with open(filepath, "rb") as f:
                    files = {"file": (filename, f, "text/plain")}
                    data = {"user": "user"}
                    response = await client.post(url, headers=self.headers, files=files, data=data)
                    response.raise_for_status()
                    file_id = response.json().get("id")
                    logger.info(f"Uploaded {filename} -> {file_id}")
                    return file_id
        except Exception as e:
            logger.error(f"Upload failed for {filename}: {e}")
            return None

    async def run_workflow(self, file_id: str, output_pdf_path: str) -> bool:
        """Runs the workflow by sending the file_id to the 'text' input variable."""
        url = f"{self.base_url}/workflows/run"
        
        # This payload now correctly sends the file object to the 'text' variable.
        payload = {
            "user": "user",
            "response_mode": "blocking",
            "inputs": {
                "text": [
                    {
                        "type": "document",
                        "transfer_method": "local__file",
                        "upload_file_id": file_id
                    }
                ]
            }
        }

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
                async with client.stream("POST", url, headers=self.headers, json=payload) as response:
                    response.raise_for_status()
                    
                    if "application/pdf" in response.headers.get("content-type", ""):
                        with open(output_pdf_path, "wb") as f:
                            async for chunk in response.aiter_bytes():
                                f.write(chunk)
                        logger.success(f"Successfully saved PDF summary to {output_pdf_path}")
                        return True
                    else:
                        error_text = await response.aread()
                        logger.error(f"Workflow did not return a PDF. Response: {error_text.decode()}")
                        return False
        except Exception as e:
            logger.error(f"Workflow run failed for file_id {file_id}: {e}")
            return False
