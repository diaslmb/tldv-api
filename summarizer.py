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
        url = f"{self.base_url}/workflows/run"
        payload = {
            "user": "user",
            "response_mode": "blocking",
            "inputs": {
                "text": [{"type": "document", "transfer_method": "local_file", "upload_file_id": file_id}]
            }
        }

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
                # 1. Run the workflow and get the JSON response
                response = await client.post(url, headers=self.headers, json=payload)
                response.raise_for_status()
                response_data = response.json()

                # 2. Extract the file URL from the JSON response
                outputs = response_data.get("data", {}).get("outputs", {})
                if "summary" in outputs and outputs["summary"]:
                    file_info = outputs["summary"][0]
                    download_url = file_info.get("url")
                    
                    if not download_url:
                        logger.error("Could not find download URL in workflow response.")
                        return False

                    # 3. Download the actual PDF file from the URL
                    full_download_url = f"https://shai.pro{download_url}"
                    logger.info(f"Downloading summary from {full_download_url}")
                    
                    async with client.stream("GET", full_download_url, headers=self.headers) as download_response:
                        download_response.raise_for_status()
                        with open(output_pdf_path, "wb") as f:
                            async for chunk in download_response.aiter_bytes():
                                f.write(chunk)
                        logger.success(f"Successfully saved PDF summary to {output_pdf_path}")
                        return True
                else:
                    logger.error("Workflow response did not contain the expected 'summary' output.")
                    return False
        except Exception as e:
            logger.error(f"Workflow run or download failed for file_id {file_id}: {e}")
            return False
